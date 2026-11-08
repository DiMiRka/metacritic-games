"""Перевод описания игры на русский: когда нужен и как сохранить.

Оригинал остаётся в games.description: по нему считаются эмбеддинги и похожие игры.
Ключ перевода — sha256 от версии промпта, модели и исходного текста: изменилось хоть что-то,
и перевод делается заново, иначе повторный обход его не трогает
"""

import hashlib

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import Game
from app.enrichment.llm import TRANSLATE_PROMPT_VERSION


def translation_key(description: str, model: str) -> str:
    raw = f"{TRANSLATE_PROMPT_VERSION}\x1e{model}\x1e{description}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def translation_outdated(session: AsyncSession, game_id: int, model: str) -> bool:
    row = (
        await session.execute(
            select(Game.description, Game.description_ru_key).where(Game.id == game_id)
        )
    ).first()
    if row is None or not row.description:
        return False
    return row.description_ru_key != translation_key(row.description, model)


async def save_translation(
    session: AsyncSession, game_id: int, source: str, text: str, model: str
) -> bool:
    """Сохраняет перевод, только если описание не поменялось, пока шёл запрос к LLM"""
    result = await session.execute(
        update(Game)
        .where(Game.id == game_id, Game.description == source)
        .values(description_ru=text, description_ru_key=translation_key(source, model))
    )
    return result.rowcount == 1
