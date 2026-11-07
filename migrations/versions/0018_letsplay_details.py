"""letsplays: ролик, расшифровка и заключение подробнее; этап letsplay в run_items

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    ("video_id", sa.String(20)),
    ("channel", sa.Text()),
    ("duration_seconds", sa.Integer()),
    ("transcript_source", sa.String(10)),
    ("language", sa.String(16)),
    ("verdict", sa.String(10)),
    ("model", sa.String(100)),
    ("prompt_version", sa.Integer()),
    ("input_tokens", sa.Integer()),
    ("output_tokens", sa.Integer()),
    ("checked_at", sa.DateTime(timezone=True)),
]
STAGES_OLD = "'scrape', 'summarize_critic', 'summarize_user', 'embed'"
STAGES_NEW = STAGES_OLD + ", 'letsplay'"


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column("letsplays", sa.Column(name, type_))
    op.create_check_constraint(
        op.f("ck_letsplays_transcript_source"), "letsplays", "transcript_source IN ('captions', 'whisper')"
    )
    op.create_check_constraint(
        op.f("ck_letsplays_verdict"), "letsplays", "verdict IN ('positive', 'mixed', 'negative')"
    )
    op.drop_constraint(op.f("ck_run_items_stage"), "run_items", type_="check")
    op.create_check_constraint(op.f("ck_run_items_stage"), "run_items", f"stage IN ({STAGES_NEW})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_run_items_stage"), "run_items", type_="check")
    op.execute("DELETE FROM run_items WHERE stage = 'letsplay'")
    op.create_check_constraint(op.f("ck_run_items_stage"), "run_items", f"stage IN ({STAGES_OLD})")
    op.drop_constraint(op.f("ck_letsplays_verdict"), "letsplays", type_="check")
    op.drop_constraint(op.f("ck_letsplays_transcript_source"), "letsplays", type_="check")
    for name, _ in reversed(COLUMNS):
        op.drop_column("letsplays", name)
