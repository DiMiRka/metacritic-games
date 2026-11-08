from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base, created_column, enum_check, updated_column


class CrawlPhase(StrEnum):
    NEW_RELEASES = "new_releases"
    BROWSE = "browse"


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"
    MANUAL = "manual"


class RunItemStage(StrEnum):
    SCRAPE = "scrape"
    SUMMARIZE_CRITIC = "summarize_critic"
    SUMMARIZE_USER = "summarize_user"
    EMBED = "embed"
    LETSPLAY = "letsplay"
    LETSPLAY_CONCLUDE = "letsplay_conclude"
    TRANSLATE = "translate"


class RunItemStatus(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class WorkerStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class CrawlState(Base):
    __tablename__ = "crawl_state"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    phase: Mapped[str] = mapped_column(String(20), default=CrawlPhase.NEW_RELEASES)
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = updated_column()

    __table_args__ = (enum_check("phase", CrawlPhase),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = created_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Кто нажал «Запустить сейчас», у прохода по расписанию NULL
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        enum_check("trigger", RunTrigger),
        enum_check("status", RunStatus),
        Index("ix_runs_started_at", "started_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    current_task: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = updated_column()

    __table_args__ = (enum_check("status", WorkerStatus),)


class RunItem(Base):
    """Одна попытка одного этапа обработки игры в рамках прохода"""

    __tablename__ = "run_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL")
    )
    slug: Mapped[str | None] = mapped_column(String(255))
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id", ondelete="SET NULL"))
    stage: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))
    error: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_column()

    __table_args__ = (
        enum_check("stage", RunItemStage),
        enum_check("status", RunItemStatus),
        Index("ix_run_items_run_id", "run_id"),
        Index("ix_run_items_slug_created_at", "slug", "created_at"),
        Index("ix_run_items_created_at", "created_at"),
    )


class RawPage(Base):
    """Сжатый набор сырых ответов по игре; новая запись появляется только при новом hash"""

    __tablename__ = "raw_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    body: Mapped[bytes] = mapped_column(LargeBinary)
    # Размер до сжатия
    size: Mapped[int] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = created_column()
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("game_id", "content_hash", name="uq_raw_pages_game_id_content_hash"),
        Index("ix_raw_pages_game_id_last_seen_at", "game_id", "last_seen_at"),
    )
