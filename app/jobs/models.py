from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
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

from app.core.models import Base, created_column, enum_check, updated_column


class JobKind(StrEnum):
    COLLECT = "collect"
    SCRAPE_GAME = "scrape_game"
    SUMMARIZE = "summarize"
    EMBED = "embed"
    LETSPLAY = "letsplay"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(String(10), default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Одна и та же работа не попадёт в очередь дважды, пока прошлая не завершилась
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        enum_check("kind", JobKind),
        enum_check("status", JobStatus),
        Index(
            "ix_jobs_pick",
            text("priority DESC"),
            "run_after",
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "uq_jobs_dedupe_key_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_jobs_run_id", "run_id"),
        Index("ix_jobs_kind_status", "kind", "status"),
    )


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Breaker(Base):
    __tablename__ = "breakers"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    state: Mapped[str] = mapped_column(String(10), default=BreakerState.CLOSED)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    opened_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_column()

    __table_args__ = (enum_check("state", BreakerState),)
