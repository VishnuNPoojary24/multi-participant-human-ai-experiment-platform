from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now_utc():
    return datetime.now(timezone.utc)


class ExperimentSession(Base):
    __tablename__ = "experiment_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    participants: Mapped[list["Participant"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    votes: Mapped[list["Vote"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (UniqueConstraint("session_id", "participant_id"), UniqueConstraint("session_id", "human_key"))
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("experiment_sessions.id", ondelete="CASCADE"))
    participant_id: Mapped[str] = mapped_column(String(1))
    kind: Mapped[str] = mapped_column(String(8))
    human_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    joined: Mapped[bool] = mapped_column(default=False)
    session: Mapped[ExperimentSession] = relationship(back_populates="participants")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("experiment_sessions.id", ondelete="CASCADE"))
    round_number: Mapped[int] = mapped_column(Integer)
    participant_id: Mapped[str] = mapped_column(String(1))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    session: Mapped[ExperimentSession] = relationship(back_populates="messages")


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("session_id", "round_number", "voter_id", name="uq_vote_per_round"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("experiment_sessions.id", ondelete="CASCADE"))
    round_number: Mapped[int] = mapped_column(Integer)
    voter_id: Mapped[str] = mapped_column(String(1))
    candidate_id: Mapped[str] = mapped_column(String(1))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    session: Mapped[ExperimentSession] = relationship(back_populates="votes")
