import hashlib

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.models import ReviewSource, ReviewSummary
from app.enrichment.llm import PROMPT_VERSION, Summarizer
from app.scraper.parsers import Review

MIN_REVIEWS = 3


def reviews_hash(reviews: list[Review]) -> str:
    """Не зависит от порядка отзывов, меняется при появлении нового текста"""
    digest = hashlib.sha256()
    for text in sorted(r.text for r in reviews):
        digest.update(text.encode())
        digest.update(b"\x1e")
    return digest.hexdigest()


async def summary_outdated(
    session: AsyncSession, game_id: int, source: ReviewSource, reviews: list[Review], model: str
) -> bool:
    """Нужно ли пересчитать резюме: отзывов достаточно, а их набор, модель или промпт другие"""
    if len(reviews) < MIN_REVIEWS:
        return False
    existing = (
        await session.execute(
            select(
                ReviewSummary.reviews_hash, ReviewSummary.model, ReviewSummary.prompt_version
            ).where(ReviewSummary.game_id == game_id, ReviewSummary.source == source)
        )
    ).first()
    return existing != (reviews_hash(reviews), model, PROMPT_VERSION)


async def update_summary(
    session_factory: async_sessionmaker[AsyncSession],
    summarizer: Summarizer,
    game_id: int,
    title: str,
    source: ReviewSource,
    reviews: list[Review],
) -> bool:
    """Пересчитывает резюме, только если набор отзывов, модель или промпт изменились"""
    async with session_factory() as session:
        if not await summary_outdated(session, game_id, source, reviews, summarizer.model):
            return False

    summary = await summarizer.summarize(title, source, reviews)

    values = {
        "game_id": game_id,
        "source": source,
        "likes": summary.digest.likes,
        "dislikes": summary.digest.dislikes,
        "reviews_count": len(reviews),
        "reviews_hash": reviews_hash(reviews),
        "model": summarizer.model,
        "prompt_version": PROMPT_VERSION,
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
    }
    stmt = insert(ReviewSummary).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_summaries_game_id_source",
        set_={
            **{k: stmt.excluded[k] for k in values if k not in ("game_id", "source")},
            "generated_at": func.now(),
        },
    )
    async with session_factory() as session, session.begin():
        await session.execute(stmt)
    return True
