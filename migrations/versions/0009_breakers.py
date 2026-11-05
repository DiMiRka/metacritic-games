"""breakers: circuit breaker state shared by all workers

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "breakers",
        sa.Column("name", sa.String(30), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("opened_until", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("name", name="pk_breakers"),
        sa.CheckConstraint(
            "state IN ('closed', 'open', 'half_open')", name=op.f("ck_breakers_state")
        ),
    )


def downgrade() -> None:
    op.drop_table("breakers")
