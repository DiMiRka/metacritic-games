from datetime import datetime

from sqlalchemy import delete, func, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.embeddings import Embedder, embedding_text
from app.llm import Summarizer, SummaryError
from app.models import Game, GamePlatform, ReviewSource
from app.scraper.client import MetacriticClient
from app.scraper.parsers import (
    GameData,
    PlatformScore,
    Review,
    parse_game,
    parse_reviews,
    parse_user_score,
)
from app.summaries import update_summary

REVIEWS_LIMIT = 30


class GameNotFoundError(Exception):
    pass


async def fetch_game(client: MetacriticClient, slug: str) -> GameData:
    data = parse_game(await client.game(slug))
    if data is None:
        raise GameNotFoundError(slug)

    platforms = []
    for p in data.platforms:
        if p.platform == data.lead_platform:
            userscore = data.lead_userscore
        else:
            userscore = parse_user_score(await client.user_stats(slug, p.platform))
        platforms.append(PlatformScore(p.platform, p.name, p.metascore, userscore))
    data.platforms = platforms
    return data


async def upsert_game(
    session: AsyncSession,
    data: GameData,
    processed_at: datetime,
    embedding: list[float] | None = None,
) -> tuple[int, bool]:
    values = {
        "slug": data.slug,
        "title": data.title,
        "cover_url": data.cover_url,
        "developer": data.developer,
        "description": data.description,
        "video_url": data.video_url,
        "release_date": data.release_date,
        "genres": data.genres,
        "last_processed_at": processed_at,
    }
    if embedding is not None:
        values["embedding"] = embedding
    stmt = insert(Game).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Game.slug],
        set_={**{k: stmt.excluded[k] for k in values if k != "slug"}, "updated_at": func.now()},
    ).returning(Game.id, literal_column("(xmax = 0)").label("inserted"))
    game_id, inserted = (await session.execute(stmt)).one()

    if data.platforms:
        rows = [
            {
                "game_id": game_id,
                "platform": p.platform,
                "platform_name": p.name,
                "metascore": p.metascore,
                "userscore": p.userscore,
            }
            for p in data.platforms
        ]
        pstmt = insert(GamePlatform).values(rows)
        await session.execute(
            pstmt.on_conflict_do_update(
                constraint="uq_game_platforms_game_id_platform",
                set_={
                    "platform_name": pstmt.excluded.platform_name,
                    "metascore": pstmt.excluded.metascore,
                    "userscore": pstmt.excluded.userscore,
                },
            )
        )
    await session.execute(
        delete(GamePlatform).where(
            GamePlatform.game_id == game_id,
            GamePlatform.platform.not_in([p.platform for p in data.platforms]),
        )
    )
    return game_id, bool(inserted)


async def fetch_reviews(
    client: MetacriticClient, data: GameData
) -> dict[ReviewSource, list[Review]]:
    reviews = {ReviewSource.CRITIC: [], ReviewSource.USER: []}
    if data.critic_reviews_total:
        reviews[ReviewSource.CRITIC] = parse_reviews(
            await client.critic_reviews(data.slug, REVIEWS_LIMIT)
        )
    if data.user_reviews_total:
        reviews[ReviewSource.USER] = parse_reviews(
            await client.user_reviews(data.slug, REVIEWS_LIMIT)
        )
    return reviews


async def process_game(
    session_factory: async_sessionmaker[AsyncSession],
    client: MetacriticClient,
    slug: str,
    processed_at: datetime,
    summarizer: Summarizer | None = None,
    embedder: Embedder | None = None,
) -> bool:
    data = await fetch_game(client, slug)
    reviews = await fetch_reviews(client, data) if summarizer else {}
    embedding = await embedder.embed(embedding_text(data)) if embedder else None

    async with session_factory() as session, session.begin():
        game_id, created = await upsert_game(session, data, processed_at, embedding)

    # Игра уже сохранена, сбой LLM по одному источнику не мешает второму
    failures = []
    for source, items in reviews.items():
        try:
            await update_summary(session_factory, summarizer, game_id, data.title, source, items)
        except Exception as exc:
            failures.append(f"{source}: {type(exc).__name__}: {exc}")
    if failures:
        raise SummaryError("; ".join(failures))
    return created
