"""move runs.errors into run_items, counters are aggregated from run_items

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Каждая запись старого массива ошибок становится упавшей попыткой этапа scrape
    op.execute(
        """
        INSERT INTO run_items (run_id, slug, stage, status, error, attempt, created_at)
        SELECT r.id, e->>'slug', 'scrape', 'failed', e->>'error', 1, r.started_at
        FROM runs r, jsonb_array_elements(r.errors) AS e
        """
    )
    op.drop_column("runs", "errors")
    # Счётчики больше не хранятся: монитор считает их агрегатом из run_items
    op.drop_column("runs", "processed")
    op.drop_column("runs", "created")
    op.drop_column("runs", "updated")


def downgrade() -> None:
    for name in ("processed", "created", "updated"):
        op.add_column(
            "runs", sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
    op.add_column(
        "runs",
        sa.Column("errors", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False),
    )
