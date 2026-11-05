"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("cover_url", sa.Text()),
        sa.Column("developer", sa.String(500)),
        sa.Column("description", sa.Text()),
        sa.Column("video_url", sa.Text()),
        sa.Column("release_date", sa.Date()),
        sa.Column(
            "genres",
            postgresql.ARRAY(sa.String(100)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(384)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_processed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name="pk_games"),
        sa.UniqueConstraint("slug", name="uq_games_slug"),
    )
    op.create_index("ix_games_last_processed_at", "games", ["last_processed_at"])
    op.create_index("ix_games_release_date", "games", ["release_date"])
    op.create_index(
        "ix_games_title_trgm",
        "games",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_games_embedding_hnsw",
        "games",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "game_platforms",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(100), nullable=False),
        sa.Column("platform_name", sa.String(100), nullable=False),
        sa.Column("metascore", sa.Integer()),
        sa.Column("userscore", sa.Float()),
        sa.PrimaryKeyConstraint("id", name="pk_game_platforms"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_game_platforms_game_id_games",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", "platform", name="uq_game_platforms_game_id_platform"),
    )
    op.create_index("ix_game_platforms_platform", "game_platforms", ["platform"])

    op.create_table(
        "review_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("likes", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("dislikes", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("reviews_count", sa.Integer(), nullable=False),
        sa.Column("reviews_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column(
            "generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_summaries"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_review_summaries_game_id_games",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", "source", name="uq_review_summaries_game_id_source"),
        sa.CheckConstraint("source IN ('critic', 'user')", name=op.f("ck_review_summaries_source")),
    )

    op.create_table(
        "letsplays",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("video_url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("views", sa.BigInteger()),
        sa.Column("transcript", sa.Text()),
        sa.Column("conclusion", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_letsplays"),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_letsplays_game_id_games", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("game_id", name="uq_letsplays_game_id"),
    )
    op.create_index("ix_letsplays_status", "letsplays", ["status"])

    op.create_table(
        "crawl_state",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("next_page", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("day", name="pk_crawl_state"),
        sa.CheckConstraint("phase IN ('new_releases', 'browse')", name=op.f("ck_crawl_state_phase")),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("created", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.CheckConstraint("trigger IN ('schedule', 'manual')", name=op.f("ck_runs_trigger")),
    )
    op.create_index("ix_runs_started_at", "runs", ["started_at"])

    op.create_table(
        "worker_heartbeats",
        sa.Column("worker", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_task", sa.Text()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("worker", name="pk_worker_heartbeats"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("runs")
    op.drop_table("crawl_state")
    op.drop_table("letsplays")
    op.drop_table("review_summaries")
    op.drop_table("game_platforms")
    op.drop_table("games")
