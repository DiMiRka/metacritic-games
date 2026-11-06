"""raw_pages: compressed raw Metacritic responses per game

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_pages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_pages"),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_raw_pages_game_id_games", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("game_id", "content_hash", name="uq_raw_pages_game_id_content_hash"),
    )
    op.create_index(
        "ix_raw_pages_game_id_last_seen_at", "raw_pages", ["game_id", "last_seen_at"]
    )


def downgrade() -> None:
    op.drop_table("raw_pages")
