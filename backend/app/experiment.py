import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai_agent import GroqParticipant
from .config import AppSettings
from .models import ExperimentSession, Message, Participant, Vote


class ExperimentManager:
    """Coordinates session transitions in one backend process."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.ai_responder = GroqParticipant()
        self.connections: dict[str, set[WebSocket]] = defaultdict(set)
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.timer_tasks: dict[str, asyncio.Task] = {}

    async def connect(self, session_id: str, socket: WebSocket):
        await socket.accept()
        self.connections[session_id].add(socket)

    def disconnect(self, session_id: str, socket: WebSocket):
        self.connections[session_id].discard(socket)

    async def broadcast(self, session_id: str, event: dict):
        for socket in list(self.connections[session_id]):
            try:
                await socket.send_json(event)
            except Exception:
                self.disconnect(session_id, socket)

    def state(self, db: Session, session_id: str):
        session = db.get(ExperimentSession, session_id)
        if session is None:
            return None
        participants = db.scalars(select(Participant).where(Participant.session_id == session_id).order_by(Participant.participant_id)).all()
        messages = db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.created_at)).all()
        votes = db.scalars(select(Vote).where(Vote.session_id == session_id)).all()
        return {"type": "state", "session_id": session.id, "status": session.status, "round": session.current_round,
                "round_duration_seconds": self.settings.experiment.round_duration_seconds,
                "participants": [{"id": p.participant_id} for p in participants],
                "messages": [{"id": m.id, "round": m.round_number, "participant_id": m.participant_id,
                              "content": m.content, "created_at": m.created_at.isoformat()} for m in messages],
                "voted": sorted({v.voter_id for v in votes if v.round_number == session.current_round})}

    async def maybe_start(self, db: Session, session_id: str):
        started = False
        async with self.locks[session_id]:
            session = db.scalar(select(ExperimentSession).where(ExperimentSession.id == session_id).with_for_update())
            participants = db.scalars(select(Participant).where(Participant.session_id == session_id)).all()
            humans_ready = all(p.joined for p in participants if p.kind == "human")
            if session and session.status == "waiting" and humans_ready:
                session.status = "discussion"
                session.current_round = 1
                db.commit()
                await self.broadcast(session_id, {"type": "experiment_started", "round": 1, "duration_seconds": self.settings.experiment.round_duration_seconds})
                self.timer_tasks[session_id] = asyncio.create_task(self._round_timer(session_id, 1))
                started = True
        if started:
            await self.ai_message(session_id, 1)

    async def _round_timer(self, session_id: str, round_number: int):
        await asyncio.sleep(self.settings.experiment.round_duration_seconds)
        db = self._db_factory()
        try:
            await self.end_round(db, session_id, round_number)
        finally:
            db.close()

    _db_factory = None

    async def ai_message(self, session_id: str, round_number: int):
        db = self._db_factory()
        try:
            agents = db.scalars(select(Participant).where(Participant.session_id == session_id, Participant.kind == "ai").order_by(Participant.participant_id)).all()
            for ai in agents:
                candidates = db.scalars(select(Participant).where(Participant.session_id == session_id, Participant.participant_id != ai.participant_id).order_by(Participant.participant_id)).all()
                roster = db.scalars(select(Participant).where(Participant.session_id == session_id)).all()
                human_ids = {item.participant_id for item in roster if item.kind == "human"}
                history_rows = db.scalars(select(Message).where(Message.session_id == session_id, Message.round_number <= round_number).order_by(Message.created_at)).all()
                history = [{"participant_id": item.participant_id, "content": item.content} for item in history_rows]
                db.rollback()  # Release the read transaction while awaiting the model stream.
                streamed_any = False

                if self.settings.ai_agent.response_delay_seconds:
                    await asyncio.sleep(self.settings.ai_agent.response_delay_seconds)

                async def on_token(token: str):
                    nonlocal streamed_any
                    streamed_any = True
                    await self.broadcast(session_id, {"type": "ai_message_delta", "round": round_number,
                        "participant_id": ai.participant_id, "content": token})

                if self.settings.ai_agent.strategy == "groq":
                    response = await self.ai_responder.respond(ai.participant_id, round_number, history,
                        [item.participant_id for item in candidates], human_ids, on_token)
                else:
                    response = None

                session = db.get(ExperimentSession, session_id)
                if not session or session.status != "discussion" or session.current_round != round_number:
                    if streamed_any:
                        await self.broadcast(session_id, {"type": "ai_message_reset", "round": round_number, "participant_id": ai.participant_id})
                    continue
                if not response:
                    if streamed_any:
                        await self.broadcast(session_id, {"type": "ai_message_reset", "round": round_number, "participant_id": ai.participant_id})
                    human_history = [item for item in history if item["participant_id"] in human_ids]
                    if not history:
                        response = f"Hello, I'm Player {ai.participant_id}. What qualities do you think matter most in a Decision Maker?"
                    elif not human_history:
                        response = "Clear explanations and fair listening could help the group decide. What do you think matters most?"
                    else:
                        response = "Let's keep our discussion focused on the Decision Maker choice. Which qualities should we prioritize when comparing the candidates?"

                msg = Message(session_id=session_id, round_number=round_number, participant_id=ai.participant_id, content=response)
                db.add(msg); db.commit(); db.refresh(msg)
                await self.broadcast(session_id, {"type": "message", "message": {"id": msg.id, "round": msg.round_number,
                    "participant_id": msg.participant_id, "content": msg.content, "created_at": msg.created_at.isoformat()}})
        finally:
            db.close()

    async def end_round(self, db: Session, session_id: str, round_number: int):
        async with self.locks[session_id]:
            session = db.get(ExperimentSession, session_id)
            if not session or session.status != "discussion" or session.current_round != round_number:
                return
            session.status = "voting"
            db.commit()
            await self.broadcast(session_id, {"type": "round_ended", "round": round_number})
            await self.ai_vote(db, session_id, round_number)
            await self.maybe_finish_round(db, session_id, round_number)

    async def ai_vote(self, db: Session, session_id: str, round_number: int):
        agents = db.scalars(select(Participant).where(Participant.session_id == session_id, Participant.kind == "ai").order_by(Participant.participant_id)).all()
        candidates = db.scalars(select(Participant).where(Participant.session_id == session_id).order_by(Participant.participant_id)).all()
        for index, ai in enumerate(agents):
            if candidates:
                candidate = candidates[(index + round_number - 1) % len(candidates)]
                db.add(Vote(session_id=session_id, round_number=round_number, voter_id=ai.participant_id, candidate_id=candidate.participant_id))
        db.commit()

    async def maybe_finish_round(self, db: Session, session_id: str, round_number: int):
        session = db.scalar(select(ExperimentSession).where(ExperimentSession.id == session_id).with_for_update())
        if not session or session.status != "voting" or session.current_round != round_number:
            return
        votes = db.scalars(select(Vote).where(Vote.session_id == session_id, Vote.round_number == round_number)).all()
        if len(votes) < 3:
            db.rollback()
            await self.broadcast(session_id, {"type": "vote_update", "round": round_number, "submitted": len(votes), "required": 3})
            return
        if round_number < self.settings.experiment.rounds:
            session.current_round = round_number + 1
            session.status = "discussion"
            db.commit()
            await self.broadcast(session_id, {"type": "round_started", "round": round_number + 1, "duration_seconds": self.settings.experiment.round_duration_seconds})
            self.timer_tasks[session_id] = asyncio.create_task(self._round_timer(session_id, round_number + 1))
            await self.ai_message(session_id, round_number + 1)
            return
        totals = Counter(v.candidate_id for v in db.scalars(select(Vote).where(Vote.session_id == session_id)).all())
        winner = sorted(("A", "B", "C"), key=lambda candidate: (-totals[candidate], candidate))[0]
        session.status = "complete"
        db.commit()
        await self.broadcast(session_id, {"type": "results", "round": round_number,
            "votes": {candidate: totals[candidate] for candidate in ("A", "B", "C")}, "winner": winner,
            "strategy": "Aggregate both rounds; ties go to the alphabetically first participant ID."})

    async def add_vote(self, db: Session, session_id: str, voter_id: str, candidate_id: str):
        async with self.locks[session_id]:
            session = db.scalar(select(ExperimentSession).where(ExperimentSession.id == session_id).with_for_update())
            if not session or session.status != "voting":
                raise ValueError("Voting is not open")
            ids = {p.participant_id for p in db.scalars(select(Participant).where(Participant.session_id == session_id)).all()}
            if voter_id not in ids or candidate_id not in ids:
                raise ValueError("Unknown voter or candidate")
            if db.scalar(select(Vote.id).where(Vote.session_id == session_id, Vote.round_number == session.current_round, Vote.voter_id == voter_id)):
                raise ValueError("Participant has already voted this round")
            db.add(Vote(session_id=session_id, round_number=session.current_round, voter_id=voter_id, candidate_id=candidate_id))
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise ValueError("Participant has already voted this round") from exc
            round_number = session.current_round
        await self.maybe_finish_round(db, session_id, round_number)

