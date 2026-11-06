"""score_history: changes of metascore and userscore per platform

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "score_history",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(100), nullable=False),
        sa.Column("metascore", sa.Integer()),
        sa.Column("userscore", sa.Float()),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_score_history"),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_score_history_game_id_games", ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_score_history_game_id_platform_observed_at",
        "score_history",
        ["game_id", "platform", "observed_at"],
    )
    # Текущие оценки становятся первой точкой истории
    op.execute(
        """
        INSERT INTO score_history (game_id, platform, metascore, userscore, observed_at)
        SELECT p.game_id, p.platform, p.metascore, p.userscore, coalesce(g.last_processed_at, now())
        FROM game_platforms p JOIN games g ON g.id = p.game_id
        """
    )


def downgrade() -> None:
    op.drop_table("score_history")
