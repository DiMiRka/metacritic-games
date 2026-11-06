"""users.totp_secret шифруется, резервные коды TOTP

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Шифротекст длиннее base32-секрета. Сами значения перешифровывает web при старте:
    # ключ есть только у него, у сервиса migrate SECRET_KEY нет
    op.alter_column("users", "totp_secret", type_=sa.Text(), existing_type=sa.String(64))
    op.create_table(
        "totp_recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lookup", sa.String(8), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_totp_recovery_codes_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_totp_recovery_codes"),
    )
    op.create_index("uq_totp_recovery_codes_user_id_lookup", "totp_recovery_codes", ["user_id", "lookup"], unique=True)


def downgrade() -> None:
    op.drop_table("totp_recovery_codes")
    op.alter_column("users", "totp_secret", type_=sa.String(64), existing_type=sa.Text())
