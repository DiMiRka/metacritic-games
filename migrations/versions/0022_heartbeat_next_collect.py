"""Время следующего прохода в сигнале воркера: отдельным полем, а не текстом в current_task,
чтобы браузер показал его в часовом поясе пользователя

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("worker_heartbeats", sa.Column("next_collect_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("worker_heartbeats", "next_collect_at")
