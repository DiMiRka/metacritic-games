import asyncio
import hashlib
import threading
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import EMBEDDING_DIM, Game
from app.core.config import settings
from app.scraper.parsers import GameData

MAX_DESCRIPTION_CHARS = 1500


class Embedder(Protocol):
    model: str

    async def embed(self, text: str) -> list[float]: ...


def embedding_text(game: GameData) -> str:
    parts = [game.title]
    if game.genres:
        parts.append("Genres: " + ", ".join(game.genres))
    if game.developer:
        parts.append(f"Developer: {game.developer}")
    if game.description:
        parts.append(game.description[:MAX_DESCRIPTION_CHARS])
    return ". ".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def embedding_outdated(session: AsyncSession, game_id: int, model: str) -> bool:
    """Нужно ли пересчитать эмбеддинг: его нет, либо поменялись текст игры или модель"""
    game = await session.scalar(select(Game).where(Game.id == game_id))
    if game is None:
        return False
    return (
        game.embedding is None
        or game.embedding_model != model
        or game.embedding_hash != text_hash(embedding_text(game))
    )


class FastEmbedder:
    """Локальная модель на ONNX, грузится при первом вызове и работает в отдельном потоке"""

    def __init__(self, model_name: str | None = None, cache_dir: str | None = None) -> None:
        self.model = self._model_name = model_name or settings.embedding_model
        self._cache_dir = cache_dir or settings.embedding_cache_dir
        self._model = None
        self._lock = threading.Lock()

    def _embed_sync(self, text: str) -> list[float]:
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(self._model_name, cache_dir=self._cache_dir)
            vector = next(iter(self._model.embed([text])))
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"{self._model_name} даёт {len(vector)} измерений, ждём {EMBEDDING_DIM}"
            )
        return vector.tolist()

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed_sync, text)
