import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import AppSettings, load_config
from app.database import Base
from app.experiment import ExperimentManager
from app.models import ExperimentSession, Participant, Vote

ROOT = Path(__file__).resolve().parents[2]


def make_settings():
    settings = load_config(ROOT / "config.yml")
    settings.ai_agent.strategy = "simple"  # Unit tests never make external API calls.
    settings.ai_agent.response_delay_seconds = 0
    return settings


def setup_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed(db, settings):
    db.add(ExperimentSession(id="test", name=settings.experiment.name, status="discussion", current_round=1))
    for p in settings.participants:
        db.add(Participant(session_id="test", participant_id=p.id, kind=p.type, joined=True))
    db.commit()


def test_yaml_configuration_loads_expected_roster():
    settings = make_settings()
    assert len(settings.participants) == 3
    assert {p.id for p in settings.participants} == {"A", "B", "C"}


def test_configuration_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        AppSettings.model_validate({"experiment": {"name": "x", "participants": 3, "rounds": 2, "round_duration_seconds": 60},
            "participants": [{"id": "A", "type": "human"}, {"id": "A", "type": "human"}, {"id": "C", "type": "ai"}],
            "ai_agent": {"enabled": True, "strategy": "simple"}})


def test_configuration_requires_a_human_slot():
    with pytest.raises(ValueError, match="at least one human"):
        AppSettings.model_validate({"experiment": {"name": "x", "participants": 3, "rounds": 2, "round_duration_seconds": 60},
            "participants": [{"id": "A", "type": "ai"}, {"id": "B", "type": "ai"}, {"id": "C", "type": "ai"}],
            "ai_agent": {"enabled": True, "strategy": "simple"}})


@pytest.mark.parametrize("participant_types", [
    ["human", "human", "human"],
    ["human", "human", "ai"],
    ["human", "ai", "ai"],
])
def test_all_official_participant_conditions_are_valid(participant_types):
    settings = AppSettings.model_validate({"experiment": {"name": "x", "participants": 3, "rounds": 2, "round_duration_seconds": 60},
        "participants": [{"id": player_id, "type": participant_type} for player_id, participant_type in zip("ABC", participant_types)],
        "ai_agent": {"enabled": True, "strategy": "simple"}})
    assert len(settings.participants) == 3


def test_participant_allocation_and_waiting_start_condition():
    settings = make_settings(); factory = setup_db(); db = factory(); seed(db, settings)
    manager = ExperimentManager(settings); manager._db_factory = factory
    state = manager.state(db, "test")
    assert len(state["participants"]) == 3
    assert all(set(participant) == {"id"} for participant in state["participants"])
    assert "human_required" not in state and "human_joined" not in state
    asyncio.run(manager.maybe_start(db, "test"))
    assert db.get(ExperimentSession, "test").status == "discussion"
    db.close()


def test_waiting_room_does_not_start_until_all_humans_join():
    settings = make_settings(); factory = setup_db(); db = factory()
    db.add(ExperimentSession(id="test", name=settings.experiment.name))
    last_human = next(p.id for p in reversed(settings.participants) if p.type == "human")
    for p in settings.participants:
        db.add(Participant(session_id="test", participant_id=p.id, kind=p.type, joined=(p.type == "ai" or (p.type == "human" and p.id != last_human))))
    db.commit(); manager = ExperimentManager(settings); manager._db_factory = factory
    asyncio.run(manager.maybe_start(db, "test"))
    assert db.get(ExperimentSession, "test").status == "waiting"
    db.scalar(select(Participant).where(Participant.session_id == "test", Participant.kind == "human", Participant.joined.is_(False))).joined = True
    db.commit(); asyncio.run(manager.maybe_start(db, "test"))
    assert db.get(ExperimentSession, "test").current_round == 1
    db.close()


def test_session_and_participant_rows_are_created_and_unique():
    settings = make_settings(); factory = setup_db(); db = factory()
    db.add(ExperimentSession(id="test", name=settings.experiment.name))
    for p in settings.participants:
        db.add(Participant(session_id="test", participant_id=p.id, kind=p.type, joined=p.type == "ai"))
    db.commit()
    assert db.scalar(select(ExperimentSession).where(ExperimentSession.id == "test")) is not None
    db.add(Participant(session_id="test", participant_id="A", kind="human", joined=False))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback(); db.close()


def test_api_session_join_starts_once_human_slot_is_filled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    test_engine = create_engine(f"sqlite:///{tmp_path / 'api-test.db'}", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main.manager, "_db_factory", factory)
    monkeypatch.setattr(main.settings.ai_agent, "response_delay_seconds", 0)

    async def offline_groq(*args, **kwargs):
        return None

    monkeypatch.setattr(main.manager.ai_responder, "respond", offline_groq)
    with TestClient(main.app) as client:
        created = client.post("/api/sessions")
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        assert created.json()["status"] == "waiting"
        assert "human_required" not in created.json() and "human_joined" not in created.json()
        assert all(set(player) == {"id"} for player in created.json()["participants"])
        assert all(set(player) == {"id"} for player in client.get(f"/api/sessions/{session_id}/participants").json())

        joined = client.post(f"/api/sessions/{session_id}/join", json={"human_key": "human-key-0001"})
        assert joined.status_code == 200
        assert joined.json()["participant_id"] == "A"
        repeat = client.post(f"/api/sessions/{session_id}/join", json={"human_key": "human-key-0001"})
        assert repeat.status_code == 200
        assert repeat.json()["participant_id"] == "A"
        extra = client.post(f"/api/sessions/{session_id}/join", json={"human_key": "human-key-0002"})
        assert extra.status_code == 409
        assert "human" not in extra.json()["detail"].lower()

        current = client.get(f"/api/sessions/{session_id}").json()
        assert current["status"] == "discussion"
        assert current["round"] == 1
        assert len(current["participants"]) == 3
        assert all(set(player) == {"id"} for player in current["participants"])
        assert {message["participant_id"] for message in current["messages"]} == {"B", "C"}
        assert all("you are good" not in message["content"].lower() for message in current["messages"])

        with client.websocket_connect(f"/ws/{session_id}/A") as socket:
            assert socket.receive_json()["type"] == "state"
            for content in ("Hi, is everyone ready?", "Thanks, that helps."):
                socket.send_json({"type": "message", "content": content})
                events = [socket.receive_json() for _ in range(3)]
                assert [event["type"] for event in events] == ["message", "message", "message"]
                assert {event["message"]["participant_id"] for event in events} == {"A", "B", "C"}
                assert events[0]["message"]["content"] == content
                assert content in events[1]["message"]["content"]
    test_engine.dispose()


def test_vote_validation_allows_self_vote_and_prevents_duplicate():
    settings = make_settings(); factory = setup_db(); db = factory(); seed(db, settings)
    db.get(ExperimentSession, "test").status = "voting"; db.commit()
    manager = ExperimentManager(settings); manager._db_factory = factory
    asyncio.run(manager.add_vote(db, "test", "A", "A"))
    with pytest.raises(ValueError, match="already voted"):
        asyncio.run(manager.add_vote(db, "test", "A", "B"))
    vote = db.scalar(select(Vote).where(Vote.session_id == "test", Vote.round_number == 1, Vote.voter_id == "A"))
    assert vote.candidate_id == "A"
    db.close()


def test_ai_votes_use_the_full_three_candidate_roster():
    settings = make_settings(); factory = setup_db(); db = factory(); seed(db, settings)
    manager = ExperimentManager(settings); manager._db_factory = factory
    asyncio.run(manager.ai_vote(db, "test", 1))
    votes = db.scalars(select(Vote).where(Vote.session_id == "test", Vote.round_number == 1)).all()
    assert {vote.voter_id for vote in votes} == {"B", "C"}
    assert {vote.candidate_id for vote in votes} == {"A", "B"}
    db.close()


def test_round_aggregation_and_alphabetical_tie_break():
    settings = make_settings(); factory = setup_db(); db = factory(); seed(db, settings)
    db.get(ExperimentSession, "test").status = "voting"; db.commit()
    manager = ExperimentManager(settings); manager._db_factory = factory
    db.add_all([Vote(session_id="test", round_number=1, voter_id="A", candidate_id="B"),
                Vote(session_id="test", round_number=1, voter_id="B", candidate_id="A"),
                Vote(session_id="test", round_number=1, voter_id="C", candidate_id="C")])
    db.commit(); settings.experiment.rounds = 1
    asyncio.run(manager.maybe_finish_round(db, "test", 1))
    assert db.get(ExperimentSession, "test").status == "complete"
    totals = {candidate: sum(v.candidate_id == candidate for v in db.scalars(select(Vote)).all()) for candidate in "ABC"}
    assert totals == {"A": 1, "B": 1, "C": 1}
    assert sorted(totals, key=lambda candidate: (-totals[candidate], candidate))[0] == "A"
    db.close()


def test_round_transition_after_all_ballots():
    settings = make_settings(); factory = setup_db(); db = factory(); seed(db, settings)
    db.get(ExperimentSession, "test").status = "voting"; db.commit()
    manager = ExperimentManager(settings); manager._db_factory = factory
    db.add_all([Vote(session_id="test", round_number=1, voter_id="A", candidate_id="B"),
                Vote(session_id="test", round_number=1, voter_id="B", candidate_id="A"),
                Vote(session_id="test", round_number=1, voter_id="C", candidate_id="A")])
    db.commit(); asyncio.run(manager.maybe_finish_round(db, "test", 1))
    assert db.get(ExperimentSession, "test").status == "discussion"
    assert db.get(ExperimentSession, "test").current_round == 2
    db.close()
