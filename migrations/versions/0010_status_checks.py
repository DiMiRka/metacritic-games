"""CHECK for letsplays.status and worker_heartbeats.status

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_letsplays_status"),
        "letsplays",
        "status IN ('pending', 'done', 'not_found', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_worker_heartbeats_status"),
        "worker_heartbeats",
        "status IN ('idle', 'running', 'stopped')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_worker_heartbeats_status"), "worker_heartbeats", type_="check")
    op.drop_constraint(op.f("ck_letsplays_status"), "letsplays", type_="check")
