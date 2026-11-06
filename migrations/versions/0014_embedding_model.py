"""games: embedding_model and embedding_hash

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# До этой миграции эмбеддинги считала только модель по умолчанию
INITIAL_MODEL = "BAAI/bge-small-en-v1.5"


def upgrade() -> None:
    op.add_column("games", sa.Column("embedding_model", sa.String(100)))
    op.add_column("games", sa.Column("embedding_hash", sa.String(64)))
    # hash текста не восстановить без кода приложения: он пустой, и первый же обход
    # пересчитает эмбеддинг один раз, дальше одинаковый текст пропускается
    op.execute(
        sa.text("UPDATE games SET embedding_model = :model WHERE embedding IS NOT NULL").bindparams(
            model=INITIAL_MODEL
        )
    )


def downgrade() -> None:
    op.drop_column("games", "embedding_hash")
    op.drop_column("games", "embedding_model")
