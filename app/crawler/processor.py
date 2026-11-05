from datetime import datetime

from sqlalchemy import delete, func, literal_column
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Game, GamePlatform
from app.scraper.client import MetacriticClient
from app.scraper.parsers import GameData, PlatformScore, parse_game, parse_user_score


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
    session: AsyncSession, data: GameData, processed_at: datetime
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


async def process_game(
    session_factory: async_sessionmaker[AsyncSession],
    client: MetacriticClient,
    slug: str,
    processed_at: datetime,
) -> bool:
    data = await fetch_game(client, slug)
    async with session_factory() as session, session.begin():
        _, created = await upsert_game(session, data, processed_at)
    return created
