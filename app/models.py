from datetime import date, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class ReviewSource(StrEnum):
    CRITIC = "critic"
    USER = "user"


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


class LetsplayStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    NOT_FOUND = "not_found"
    FAILED = "failed"


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    cover_url: Mapped[str | None] = mapped_column(Text)
    developer: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None] = mapped_column(Text)
    release_date: Mapped[date | None] = mapped_column(Date)
    genres: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)), default=list, server_default=text("'{}'")
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    platforms: Mapped[list["GamePlatform"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", order_by="GamePlatform.platform"
    )
    summaries: Mapped[list["ReviewSummary"]] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )
    letsplay: Mapped["Letsplay | None"] = relationship(
        back_populates="game", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_games_last_processed_at", "last_processed_at"),
        Index("ix_games_release_date", "release_date"),
        Index(
            "ix_games_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_games_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class GamePlatform(Base):
    __tablename__ = "game_platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(100))
    platform_name: Mapped[str] = mapped_column(String(100))
    metascore: Mapped[int | None] = mapped_column(Integer)
    userscore: Mapped[float | None] = mapped_column(Float)

    game: Mapped[Game] = relationship(back_populates="platforms")

    __table_args__ = (
        UniqueConstraint("game_id", "platform", name="uq_game_platforms_game_id_platform"),
        Index("ix_game_platforms_platform", "platform"),
    )


class ReviewSummary(Base):
    __tablename__ = "review_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(10))
    likes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'"))
    dislikes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'"))
    reviews_count: Mapped[int] = mapped_column(Integer, default=0)
    reviews_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    generated_at: Mapped[datetime] = _created()

    game: Mapped[Game] = relationship(back_populates="summaries")

    __table_args__ = (
        UniqueConstraint("game_id", "source", name="uq_review_summaries_game_id_source"),
        CheckConstraint("source IN ('critic', 'user')", name="source"),
    )


class Letsplay(Base):
    __tablename__ = "letsplays"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), unique=True)
    video_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    views: Mapped[int | None] = mapped_column(BigInteger)
    transcript: Mapped[str | None] = mapped_column(Text)
    conclusion: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=LetsplayStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    game: Mapped[Game] = relationship(back_populates="letsplay")

    __table_args__ = (Index("ix_letsplays_status", "status"),)


class CrawlState(Base):
    __tablename__ = "crawl_state"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    phase: Mapped[str] = mapped_column(String(20), default=CrawlPhase.NEW_RELEASES)
    next_page: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (CheckConstraint("phase IN ('new_releases', 'browse')", name="phase"),)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default=text("'[]'"))

    __table_args__ = (
        CheckConstraint("trigger IN ('schedule', 'manual')", name="trigger"),
        Index("ix_runs_started_at", "started_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    current_task: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _updated()
