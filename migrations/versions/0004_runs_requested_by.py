"""who requested a manual run

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("requested_by", sa.Integer()))
    op.create_foreign_key(
        "fk_runs_requested_by_users",
        "runs",
        "users",
        ["requested_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_runs_requested_by_users", "runs", type_="foreignkey")
    op.drop_column("runs", "requested_by")
