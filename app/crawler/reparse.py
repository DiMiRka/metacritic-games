"""Пересборка без нового обхода: игр из сырых ответов, резюме и эмбеддингов по сохранённым данным"""

from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.models import Game
from app.core.errors import PermanentError
from app.crawler.raw import load_latest
from app.crawler.stages import Derived, enqueue_derived, store_bundle, stored_reviews


@dataclass
class ReparseReport:
    rebuilt: int = 0
    without_raw: int = 0
    failed: int = 0


async def reparse_games(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str | None = None,
    summary_model: str | None,
    embed_model: str | None,
) -> ReparseReport:
    """Каждая игра в своей транзакции: сбой разбора одной не мешает остальным"""
    query = select(Game.id, Game.slug, Game.last_processed_at).order_by(Game.id)
    if slug:
        query = query.where(Game.slug == slug)
    async with session_factory() as session:
        games = (await session.execute(query)).all()

    report = ReparseReport()
    for game_id, game_slug, processed_at in games:
        try:
            async with session_factory() as session, session.begin():
                bundle = await load_latest(session, game_id)
                if bundle is None:
                    report.without_raw += 1
                    continue
                # Время обхода не меняется: это пересборка, а не новый обход
                _, _, reviews = await store_bundle(
                    session, bundle, game_slug, processed_at, save=False
                )
                await enqueue_derived(
                    session,
                    game_id,
                    reviews,
                    run_id=None,
                    summary_model=summary_model,
                    embed_model=embed_model,
                )
                report.rebuilt += 1
        except PermanentError as exc:
            report.failed += 1
            logger.warning("reparse {}: {}", game_slug, exc)
    return report


async def refresh_derived(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    summary_model: str | None,
    embed_model: str | None,
) -> Derived:
    """Ставит в очередь устаревшие резюме и эмбеддинги: после смены модели или PROMPT_VERSION"""
    async with session_factory() as session:
        game_ids = (await session.scalars(select(Game.id).order_by(Game.id))).all()
    total = Derived()
    for game_id in game_ids:
        async with session_factory() as session, session.begin():
            reviews = await stored_reviews(session, game_id)
            derived = await enqueue_derived(
                session,
                game_id,
                reviews,
                run_id=None,
                summary_model=summary_model,
                embed_model=embed_model,
            )
        total.summaries += derived.summaries
        total.embeddings += derived.embeddings
    return total
