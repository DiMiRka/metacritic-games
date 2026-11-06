"""users: TOTP, login_attempts index by login

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret", sa.String(64)))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger()))
    op.create_index("ix_login_attempts_login_at", "login_attempts", ["login", "attempted_at"])


def downgrade() -> None:
    op.drop_index("ix_login_attempts_login_at", table_name="login_attempts")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_secret")
