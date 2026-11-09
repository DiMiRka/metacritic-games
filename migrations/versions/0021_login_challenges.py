"""login_challenges: второй шаг входа администратора

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_challenges",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("next_url", sa.Text(), nullable=False),
        sa.Column("ip", sa.String(64)),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_login_challenges_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_login_challenges"),
        sa.UniqueConstraint("user_id", name="uq_login_challenges_user_id"),
        sa.UniqueConstraint("token_hash", name="uq_login_challenges_token_hash"),
    )
    op.create_index("ix_login_challenges_expires_at", "login_challenges", ["expires_at"])


def downgrade() -> None:
    op.drop_table("login_challenges")
