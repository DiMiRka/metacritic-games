"""Сырые ответы Metacritic по игре: пересборка без нового обхода и пропуск неизменённых игр"""

import hashlib
import json
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawler.models import RawPage

# Блоки карточки, которые читает парсер. Остальные (цены, новости, карусель, статистика
# GameFAQs) меняются при каждом запросе: с ними hash не совпадал бы никогда
CARD_COMPONENTS = frozenset(
    {"product", "critic-score-summary", "critic-reviews", "user-score-summary", "user-reviews"}
)


@dataclass
class RawBundle:
    """Всё, из чего собирается игра: карточка, оценки игроков по платформам, отзывы"""

    card: dict[str, Any]
    user_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    critic_reviews: list[dict[str, Any]] = field(default_factory=list)
    user_reviews: list[dict[str, Any]] = field(default_factory=list)

    def canonical(self) -> bytes:
        return json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def compress(self) -> bytes:
        return zlib.compress(self.canonical(), 6)

    @classmethod
    def decompress(cls, body: bytes) -> "RawBundle":
        return cls(**json.loads(zlib.decompress(body)))


def stable_card(raw: dict[str, Any]) -> dict[str, Any]:
    components = [
        c
        for c in raw.get("components") or []
        if (c.get("meta") or {}).get("componentName") in CARD_COMPONENTS
    ]
    return {"components": components}


async def latest_hash(session: AsyncSession, game_id: int) -> str | None:
    return await session.scalar(
        select(RawPage.content_hash)
        .where(RawPage.game_id == game_id)
        .order_by(RawPage.last_seen_at.desc(), RawPage.id.desc())
        .limit(1)
    )


async def save_raw(session: AsyncSession, game_id: int, bundle: RawBundle) -> None:
    """Новая запись только при новом hash, иначе обновляется время последнего просмотра"""
    body = bundle.compress()
    stmt = insert(RawPage).values(
        game_id=game_id,
        content_hash=bundle.digest,
        body=body,
        size=len(bundle.canonical()),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_raw_pages_game_id_content_hash", set_={"last_seen_at": func.now()}
        )
    )


async def load_latest(session: AsyncSession, game_id: int) -> RawBundle | None:
    body = await session.scalar(
        select(RawPage.body)
        .where(RawPage.game_id == game_id)
        .order_by(RawPage.last_seen_at.desc(), RawPage.id.desc())
        .limit(1)
    )
    return RawBundle.decompress(body) if body is not None else None
