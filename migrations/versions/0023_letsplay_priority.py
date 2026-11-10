"""Летсплеи по приоритету Metascore и возврат задач, умерших на исчерпанной квоте YouTube

Квоты хватает примерно на 100 поисков в сутки. Задачи в очереди получают приоритет по лучшему
Metascore игры, игры без оценки уходят в конец (их задача завершится без запроса к API).
Задачи, ставшие dead только из-за квоты, возвращаются в очередь с чистым счётчиком попыток

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEST_METASCORE = """
    COALESCE(
        (SELECT max(p.metascore) FROM game_platforms p
         WHERE p.game_id = (jobs.payload->>'game_id')::int),
        -1
    )
"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE jobs SET priority = {BEST_METASCORE}
        WHERE kind = 'letsplay' AND status = 'queued'
        """
    )
    # По одной dead-задаче на игру и только если по ней нет живой: иначе нарушится
    # уникальность dedupe_key среди queued и running
    op.execute(
        f"""
        UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(),
            finished_at = NULL, locked_by = NULL, locked_until = NULL,
            priority = {BEST_METASCORE}
        WHERE id IN (
            SELECT DISTINCT ON (dedupe_key) id FROM jobs d
            WHERE d.kind = 'letsplay' AND d.status = 'dead'
              AND d.last_error LIKE '%QuotaExceededError%'
              AND NOT EXISTS (
                  SELECT 1 FROM jobs a
                  WHERE a.dedupe_key = d.dedupe_key AND a.status IN ('queued', 'running')
              )
            ORDER BY dedupe_key, id DESC
        )
        """
    )


def downgrade() -> None:
    # Данные: прежние приоритеты и статусы не восстанавливаются
    pass
