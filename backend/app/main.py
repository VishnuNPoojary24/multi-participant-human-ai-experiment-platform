import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select

from .config import load_config
from .database import Base, SessionLocal, engine
from .experiment import ExperimentManager
from .models import ExperimentSession, Message, Participant, Vote

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yml" if os.path.exists("/app/config.yml") else "config.yml")
settings = load_config(CONFIG_PATH)
manager = ExperimentManager(settings)
manager._db_factory = SessionLocal


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield
    for task in manager.timer_tasks.values():
        task.cancel()


app = FastAPI(title="Human-AI Experiment Platform", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


class JoinRequest(BaseModel):
    human_key: str = Field(min_length=8, max_length=64)


class VoteRequest(BaseModel):
    participant_id: str
    candidate_id: str


def session_or_404(db, session_id):
    session = db.get(ExperimentSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    return session


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/sessions", status_code=201)
async def create_session():
    session_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        session = ExperimentSession(id=session_id, name=settings.experiment.name)
        db.add(session)
        for participant in settings.participants:
            db.add(Participant(session_id=session_id, participant_id=participant.id, kind=participant.type, joined=participant.type == "ai"))
        db.commit()
        return manager.state(db, session_id)
    finally:
        db.close()


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    db = SessionLocal()
    try:
        session_or_404(db, session_id)
        return manager.state(db, session_id)
    finally:
        db.close()


@app.post("/api/sessions/{session_id}/join")
async def join_session(session_id: str, request: JoinRequest):
    async with manager.locks[session_id]:
        db = SessionLocal()
        try:
            session = db.scalar(select(ExperimentSession).where(ExperimentSession.id == session_id).with_for_update())
            if session is None:
                raise HTTPException(404, "Session not found")
            existing = db.scalar(select(Participant).where(Participant.session_id == session_id, Participant.human_key == request.human_key))
            if existing:
                return {"participant_id": existing.participant_id, "state": manager.state(db, session_id)}
            if session.status != "waiting":
                raise HTTPException(409, "Session has already started")
            available = db.scalars(select(Participant).where(Participant.session_id == session_id, Participant.kind == "human", Participant.joined.is_(False)).order_by(Participant.participant_id)).first()
            if available is None:
                raise HTTPException(409, "No participant seats are currently available")
            available.joined = True
            available.human_key = request.human_key
            db.commit()
            participant_id = available.participant_id
            state = manager.state(db, session_id)
        finally:
            db.close()
    await manager.broadcast(session_id, {"type": "participant_joined", "participant_id": participant_id, **state})
    start_db = SessionLocal()
    try:
        await manager.maybe_start(start_db, session_id)
    finally:
        start_db.close()
    return {"participant_id": participant_id, "state": state}


@app.get("/api/sessions/{session_id}/participants")
def get_participants(session_id: str):
    db = SessionLocal()
    try:
        session_or_404(db, session_id)
        participants = db.scalars(select(Participant).where(Participant.session_id == session_id).order_by(Participant.participant_id)).all()
        return [{"id": p.participant_id} for p in participants]
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/results")
def get_results(session_id: str):
    db = SessionLocal()
    try:
        session = session_or_404(db, session_id)
        if session.status != "complete":
            raise HTTPException(409, "Results are not available yet")
        votes = db.scalars(select(Vote).where(Vote.session_id == session_id)).all()
        totals = {candidate: sum(v.candidate_id == candidate for v in votes) for candidate in ("A", "B", "C")}
        winner = sorted(totals, key=lambda candidate: (-totals[candidate], candidate))[0]
        return {"votes": totals, "winner": winner, "strategy": "Aggregate both rounds; ties go to the alphabetically first participant ID."}
    finally:
        db.close()


@app.post("/api/sessions/{session_id}/votes")
async def submit_vote(session_id: str, request: VoteRequest):
    db = SessionLocal()
    try:
        try:
            await manager.add_vote(db, session_id, request.participant_id, request.candidate_id)
            return {"accepted": True}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    finally:
        db.close()


@app.websocket("/ws/{session_id}/{participant_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, participant_id: str):
    db = SessionLocal()
    try:
        participant = db.scalar(select(Participant).where(Participant.session_id == session_id, Participant.participant_id == participant_id))
        if not participant or participant.kind == "ai" or not participant.joined:
            await websocket.close(code=1008)
            return
    finally:
        db.close()
    await manager.connect(session_id, websocket)
    db = SessionLocal()
    try:
        await websocket.send_json(manager.state(db, session_id))
    finally:
        db.close()
    try:
        while True:
            event = await websocket.receive_json()
            db = SessionLocal()
            try:
                if event.get("type") == "message":
                    session = session_or_404(db, session_id)
                    content = str(event.get("content", "")).strip()
                    if session.status != "discussion" or not content or len(content) > 1000:
                        await websocket.send_json({"type": "error", "message": "Messages are allowed only during discussion and must be 1–1000 characters."})
                        continue
                    msg = Message(session_id=session_id, round_number=session.current_round, participant_id=participant_id, content=content)
                    db.add(msg); db.commit(); db.refresh(msg)
                    await manager.broadcast(session_id, {"type": "message", "message": {"id": msg.id, "round": msg.round_number,
                        "participant_id": msg.participant_id, "content": msg.content, "created_at": msg.created_at.isoformat()}})
                    # Let the AI use the whole turn and conversation context; keyword gates made ordinary chat feel unresponsive.
                    await manager.ai_message(session_id, session.current_round)
                elif event.get("type") == "vote":
                    try:
                        await manager.add_vote(db, session_id, participant_id, str(event.get("candidate_id", "")))
                    except ValueError as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
            finally:
                db.close()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(session_id, websocket)
