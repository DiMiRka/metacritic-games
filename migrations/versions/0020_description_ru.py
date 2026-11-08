"""Перевод описаний игр на русский: games.description_ru, задача translate

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'collect', 'scrape_game', 'summarize', 'embed', 'letsplay', 'letsplay_conclude'"
STAGES = "'scrape', 'summarize_critic', 'summarize_user', 'embed', 'letsplay', 'letsplay_conclude'"


def _replace(table: str, column: str, values: str) -> None:
    name = op.f(f"ck_{table}_{column}")
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, f"{column} IN ({values})")


def upgrade() -> None:
    op.add_column("games", sa.Column("description_ru", sa.Text()))
    op.add_column("games", sa.Column("description_ru_key", sa.String(64)))
    _replace("jobs", "kind", KINDS + ", 'translate'")
    _replace("run_items", "stage", STAGES + ", 'translate'")


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'translate'")
    op.execute("DELETE FROM run_items WHERE stage = 'translate'")
    _replace("jobs", "kind", KINDS)
    _replace("run_items", "stage", STAGES)
    op.drop_column("games", "description_ru_key")
    op.drop_column("games", "description_ru")
