from datetime import date, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base, created_column, enum_check, updated_column

EMBEDDING_DIM = 384


class ReviewSource(StrEnum):
    CRITIC = "critic"
    USER = "user"


class LetsplayStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    NOT_FOUND = "not_found"
    FAILED = "failed"


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
    # Чем и из какого текста посчитан эмбеддинг: тот же текст той же моделью не пересчитывается
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()
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
    # Версия промпта и схемы ответа: при её смене резюме пересчитываются
    prompt_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # Расход токенов на последний пересчёт, по ответу LLM
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime] = created_column()

    game: Mapped[Game] = relationship(back_populates="summaries")

    __table_args__ = (
        UniqueConstraint("game_id", "source", name="uq_review_summaries_game_id_source"),
        enum_check("source", ReviewSource),
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
    created_at: Mapped[datetime] = created_column()
    updated_at: Mapped[datetime] = updated_column()

    game: Mapped[Game] = relationship(back_populates="letsplay")

    __table_args__ = (Index("ix_letsplays_status", "status"), enum_check("status", LetsplayStatus))


class GameReview(Base):
    """Последний набор отзывов по игре и источнику, при каждом обходе заменяется целиком"""

    __tablename__ = "game_reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(10))
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    author: Mapped[str | None] = mapped_column(String(300))
    fetched_at: Mapped[datetime] = created_column()

    __table_args__ = (
        UniqueConstraint(
            "game_id", "source", "position", name="uq_game_reviews_game_id_source_position"
        ),
        enum_check("source", ReviewSource),
    )


class ScoreHistory(Base):
    """Изменения оценок по платформам: строка появляется, только когда оценка поменялась"""

    __tablename__ = "score_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(100))
    metascore: Mapped[int | None] = mapped_column(Integer)
    userscore: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = created_column()

    __table_args__ = (
        Index(
            "ix_score_history_game_id_platform_observed_at", "game_id", "platform", "observed_at"
        ),
    )
