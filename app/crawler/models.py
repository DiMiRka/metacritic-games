from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, created_column, updated_column


class CrawlPhase(StrEnum):
    NEW_RELEASES = "new_releases"
    BROWSE = "browse"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class CrawlState(Base):
    __tablename__ = "crawl_state"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    phase: Mapped[str] = mapped_column(String(20), default=CrawlPhase.NEW_RELEASES)
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = updated_column()

    __table_args__ = (CheckConstraint("phase IN ('new_releases', 'browse')", name="phase"),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = created_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default=text("'[]'"))
    # Кто нажал «Запустить сейчас», у прохода по расписанию NULL
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("trigger IN ('schedule', 'manual')", name="trigger"),
        Index("ix_runs_started_at", "started_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    current_task: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_column()
