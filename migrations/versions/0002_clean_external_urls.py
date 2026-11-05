"""clean untrusted external urls

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Хосты зафиксированы на момент миграции и совпадают с safe_url в парсере
    op.execute(
        "UPDATE games SET video_url = NULL "
        "WHERE video_url IS NOT NULL AND video_url !~ '^https://cdn\\.jwplayer\\.com/'"
    )
    op.execute(
        "UPDATE games SET cover_url = NULL "
        "WHERE cover_url IS NOT NULL AND cover_url !~ '^https://www\\.metacritic\\.com/'"
    )
    op.execute(
        "UPDATE letsplays SET video_url = NULL "
        "WHERE video_url IS NOT NULL AND video_url !~ '^https://www\\.youtube\\.com/'"
    )


def downgrade() -> None:
    pass
