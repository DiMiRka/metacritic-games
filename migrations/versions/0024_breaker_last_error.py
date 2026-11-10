"""Причина последнего сбоя в автомате: монитор показывает, почему сервис приостановлен

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("breakers", sa.Column("last_error", sa.Text()))


def downgrade() -> None:
    op.drop_column("breakers", "last_error")
