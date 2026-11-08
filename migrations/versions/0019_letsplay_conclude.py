"""Летсплей в две задачи: letsplay_conclude, статус transcribed

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'collect', 'scrape_game', 'summarize', 'embed', 'letsplay'"
STAGES = "'scrape', 'summarize_critic', 'summarize_user', 'embed', 'letsplay'"
STATUSES = "'pending', 'done', 'not_found', 'failed'"


def _replace(table: str, column: str, values: str) -> None:
    name = op.f(f"ck_{table}_{column}")
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"{column} IN ({values})")


def upgrade() -> None:
    _replace("jobs", "kind", KINDS + ", 'letsplay_conclude'")
    _replace("run_items", "stage", STAGES + ", 'letsplay_conclude'")
    _replace("letsplays", "status", "'pending', 'transcribed', 'done', 'not_found', 'failed'")


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'letsplay_conclude'")
    op.execute("DELETE FROM run_items WHERE stage = 'letsplay_conclude'")
    op.execute("UPDATE letsplays SET status = 'failed' WHERE status = 'transcribed'")
    _replace("jobs", "kind", KINDS)
    _replace("run_items", "stage", STAGES)
    _replace("letsplays", "status", STATUSES)
