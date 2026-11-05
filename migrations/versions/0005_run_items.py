"""run_items: one row per attempt of one stage

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.BigInteger()),
        sa.Column("slug", sa.String(255)),
        sa.Column("game_id", sa.Integer()),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_items"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_run_items_run_id_runs", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_run_items_game_id_games", ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "stage IN ('scrape', 'summarize_critic', 'summarize_user', 'embed')",
            name=op.f("ck_run_items_stage"),
        ),
        sa.CheckConstraint(
            "status IN ('created', 'updated', 'failed', 'skipped', 'retrying')",
            name=op.f("ck_run_items_status"),
        ),
    )
    op.create_index("ix_run_items_run_id", "run_items", ["run_id"])
    op.create_index("ix_run_items_slug_created_at", "run_items", ["slug", "created_at"])
    op.create_index("ix_run_items_created_at", "run_items", ["created_at"])


def downgrade() -> None:
    op.drop_table("run_items")
