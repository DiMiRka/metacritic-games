"""review_summaries: prompt_version and LLM usage

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Существующие резюме сделаны первой версией промпта
    op.add_column(
        "review_summaries",
        sa.Column("prompt_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column("review_summaries", sa.Column("input_tokens", sa.Integer()))
    op.add_column("review_summaries", sa.Column("output_tokens", sa.Integer()))


def downgrade() -> None:
    for name in ("output_tokens", "input_tokens", "prompt_version"):
        op.drop_column("review_summaries", name)
