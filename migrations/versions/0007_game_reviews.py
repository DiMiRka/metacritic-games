"""game_reviews and run status partial

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_reviews",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.Column("author", sa.String(300)),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_game_reviews"),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_game_reviews_game_id_games", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "game_id", "source", "position", name="uq_game_reviews_game_id_source_position"
        ),
        sa.CheckConstraint("source IN ('critic', 'user')", name=op.f("ck_game_reviews_source")),
    )
    # Проходы, оставшиеся в running от старого обходчика, к этому моменту уже не идут
    op.execute("UPDATE runs SET status = 'failed', finished_at = now() WHERE status = 'running'")
    op.create_check_constraint(
        op.f("ck_runs_status"), "runs", "status IN ('running', 'success', 'partial', 'failed')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_status"), "runs", type_="check")
    op.drop_table("game_reviews")
