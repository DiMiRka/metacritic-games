"""Этап letsplay: самый популярный летсплей игры, речь блогера текстом, заключение LLM.

Запускается после сохранения игры, если летсплея ещё нет, прошлая попытка не удалась больше
суток назад или с последнего поиска прошло LETSPLAY_REFRESH_DAYS. Тот же ролик с тем же
промптом и моделью повторно не расшифровывается: обновляются только просмотры
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.models import Game, Letsplay, LetsplayStatus
from app.core.errors import PermanentError
from app.enrichment.llm import LETSPLAY_PROMPT_VERSION, Summarizer
from app.youtube.client import LetsplaySource

# Сохраняем расшифровку целиком до этого размера: в LLM уходит меньше
MAX_STORED_TRANSCRIPT = 100_000
FAILED_RETRY_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class LetsplayResult:
    game_id: int
    changed: bool


async def letsplay_due(session: AsyncSession, game_id: int, *, refresh_days: int) -> bool:
    row = (
        await session.execute(
            select(Letsplay.status, Letsplay.checked_at, Letsplay.updated_at, func.now()).where(
                Letsplay.game_id == game_id
            )
        )
    ).first()
    if row is None:
        return True
    status, checked_at, updated_at, now = row
    if status in (LetsplayStatus.PENDING, LetsplayStatus.FAILED):
        return updated_at < now - FAILED_RETRY_AFTER or status == LetsplayStatus.PENDING
    return checked_at is None or checked_at < now - timedelta(days=refresh_days)


async def upsert_letsplay(session: AsyncSession, game_id: int, values: dict[str, Any]) -> None:
    stmt = insert(Letsplay).values(game_id=game_id, **values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Letsplay.game_id],
            set_={**{k: stmt.excluded[k] for k in values}, "updated_at": func.now()},
        )
    )


async def mark_failed(session: AsyncSession, game_id: int, error: str) -> None:
    """Задача сдалась: прежнее заключение, если было, остаётся на странице игры"""
    existing = await session.scalar(select(Letsplay.status).where(Letsplay.game_id == game_id))
    if existing == LetsplayStatus.DONE:
        await upsert_letsplay(session, game_id, {"error": error[:1000]})
    else:
        values = {"status": LetsplayStatus.FAILED, "error": error[:1000]}
        await upsert_letsplay(session, game_id, values)


class LetsplayWork:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: LetsplaySource,
        summarizer: Summarizer,
        max_transcript_chars: int,
    ) -> None:
        self.session_factory = session_factory
        self.source = source
        self.summarizer = summarizer
        self.max_chars = max_transcript_chars

    async def run(self, game_id: int) -> LetsplayResult:
        async with self.session_factory() as session:
            title = await session.scalar(select(Game.title).where(Game.id == game_id))
            current = await session.scalar(select(Letsplay).where(Letsplay.game_id == game_id))
        if title is None:
            raise PermanentError(f"игры {game_id} больше нет")

        candidate = await self.source.find(title)
        if candidate is None:
            async with self.session_factory() as session, session.begin():
                values = {
                    "status": LetsplayStatus.NOT_FOUND,
                    "error": None,
                    "checked_at": func.now(),
                }
                await upsert_letsplay(session, game_id, values)
            logger.info("летсплей для {} не найден", title)
            return LetsplayResult(game_id, changed=current is None or current.status != "not_found")

        same = (
            current is not None
            and current.status == LetsplayStatus.DONE
            and current.video_id == candidate.video_id
            and current.prompt_version == LETSPLAY_PROMPT_VERSION
            and current.model == self.summarizer.model
        )
        if same:
            async with self.session_factory() as session, session.begin():
                values = {"views": candidate.views, "checked_at": func.now(), "error": None}
                await upsert_letsplay(session, game_id, values)
            return LetsplayResult(game_id, changed=False)

        transcript = await self.source.transcript(candidate)
        conclusion = await self.summarizer.conclude(
            title, candidate.title, transcript.text[: self.max_chars]
        )
        values = {
            "video_id": candidate.video_id,
            "video_url": candidate.url,
            "title": candidate.title[:500],
            "channel": candidate.channel,
            "views": candidate.views,
            "duration_seconds": candidate.duration_seconds,
            "transcript": transcript.text[:MAX_STORED_TRANSCRIPT],
            "transcript_source": transcript.source,
            "language": (transcript.language or "")[:16] or None,
            "conclusion": conclusion.digest.conclusion.strip(),
            "verdict": conclusion.digest.verdict,
            "model": self.summarizer.model,
            "prompt_version": LETSPLAY_PROMPT_VERSION,
            "input_tokens": conclusion.input_tokens,
            "output_tokens": conclusion.output_tokens,
            "status": LetsplayStatus.DONE,
            "error": None,
            "checked_at": func.now(),
        }
        async with self.session_factory() as session, session.begin():
            await upsert_letsplay(session, game_id, values)
        logger.info(
            "летсплей {}: {} ({} просмотров, {})",
            title,
            candidate.url,
            candidate.views,
            transcript.source,
        )
        return LetsplayResult(game_id, changed=True)
