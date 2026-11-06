"""backup_runs: журнал бэкапов

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("file", sa.String(200)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("restore_checked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error", sa.Text()),
        sa.CheckConstraint("status IN ('running', 'success', 'failed')", name="ck_backup_runs_status"),
        sa.PrimaryKeyConstraint("id", name="pk_backup_runs"),
    )


def downgrade() -> None:
    op.drop_table("backup_runs")
