"""Летсплеи, две задачи очереди.

letsplay (автомат youtube): самый популярный ролик и речь блогера текстом. Расшифровка
сохраняется сразу, и в той же транзакции ставится letsplay_conclude. Тот же ролик, уже
расшифрованный раньше, второй раз не скачивается и не распознаётся.

letsplay_conclude (автомат llm): заключение по сохранённой расшифровке. Если LLM недоступна,
повторяется только этот шаг, без скачивания звука и Whisper.

Задача letsplay ставится после сохранения игры, если летсплея ещё нет, прошлая попытка не
удалась больше суток назад или с последнего поиска прошло LETSPLAY_REFRESH_DAYS.

Квоты YouTube Data API хватает примерно на 100 поисков в сутки, а обход приносит до 480 игр.
Поэтому летсплей ищется только для игр с Metascore, и очередь разбирается по убыванию оценки.
У нишевых новинок без оценок летсплеев обычно нет, поиск по ним тратил бы квоту впустую
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.catalog.models import Game, GamePlatform, Letsplay, LetsplayStatus
from app.core.config import settings
from app.core.errors import PermanentError
from app.enrichment.llm import LETSPLAY_PROMPT_VERSION, Summarizer
from app.jobs.models import JobKind
from app.jobs.queue import enqueue
from app.youtube.client import LetsplaySource
from app.youtube.errors import NoSpeechError, VideoUnavailableError
from app.youtube.search import Candidate
from app.youtube.transcript import Transcript

# Сохраняем расшифровку целиком до этого размера: в LLM уходит меньше
MAX_STORED_TRANSCRIPT = 100_000
RETRY_AFTER = timedelta(days=1)


@dataclass(frozen=True)
class LetsplayResult:
    game_id: int
    changed: bool


async def best_metascore(session: AsyncSession, game_id: int) -> int | None:
    """Лучший Metascore игры по платформам: по нему решается, искать ли летсплей и когда"""
    return await session.scalar(
        select(func.max(GamePlatform.metascore)).where(GamePlatform.game_id == game_id)
    )


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
    if status == LetsplayStatus.PENDING:
        return True
    # Заключение не получилось: задача letsplay найдёт тот же ролик и поставит только его
    if status in (LetsplayStatus.FAILED, LetsplayStatus.TRANSCRIBED):
        return updated_at < now - RETRY_AFTER
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
    """Задача сдалась: прежнее заключение, если было, остаётся на странице игры,
    сохранённая расшифровка тоже остаётся"""
    existing = await session.scalar(select(Letsplay.status).where(Letsplay.game_id == game_id))
    if existing == LetsplayStatus.DONE:
        await upsert_letsplay(session, game_id, {"error": error[:1000]})
    else:
        values = {"status": LetsplayStatus.FAILED, "error": error[:1000]}
        await upsert_letsplay(session, game_id, values)


async def enqueue_conclusion(session: AsyncSession, game_id: int, run_id: int | None) -> None:
    await enqueue(
        session,
        JobKind.LETSPLAY_CONCLUDE,
        {"game_id": game_id},
        dedupe_key=f"letsplay_conclude:{game_id}",
        run_id=run_id,
    )


class LetsplayTranscribe:
    """Задача letsplay: поиск ролика и расшифровка речи. LLM ей не нужна"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        source: LetsplaySource,
        max_candidates: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.source = source
        self.max_candidates = max_candidates or settings.letsplay_max_candidates

    async def _first_with_speech(
        self, title: str, candidates: list[Candidate]
    ) -> tuple[Candidate, Transcript]:
        """Первый ролик, в котором есть речь. Нет речи или ролик недоступен — следующий
        по популярности; блокировка YouTube и сбои сети прерывают задачу"""
        last_error: Exception | None = None
        for candidate in candidates[: self.max_candidates]:
            try:
                return candidate, await self.source.transcript(candidate)
            except (NoSpeechError, VideoUnavailableError) as exc:
                last_error = exc
                logger.info(
                    "летсплей {}: {} пропущен ({}), пробую следующий", title, candidate.url, exc
                )
        tried = min(len(candidates), self.max_candidates)
        raise NoSpeechError(f"речи нет ни в одном из {tried} роликов: {last_error}")

    async def run(self, game_id: int, run_id: int | None) -> LetsplayResult:
        async with self.session_factory() as session:
            title = await session.scalar(select(Game.title).where(Game.id == game_id))
            current = await session.scalar(select(Letsplay).where(Letsplay.game_id == game_id))
        if title is None:
            raise PermanentError(f"игры {game_id} больше нет")

        candidates = await self.source.find(title)
        if not candidates:
            async with self.session_factory() as session, session.begin():
                values = {
                    "status": LetsplayStatus.NOT_FOUND,
                    "error": None,
                    "checked_at": func.now(),
                }
                await upsert_letsplay(session, game_id, values)
            logger.info("летсплей для {} не найден", title)
            changed = current is None or current.status != LetsplayStatus.NOT_FOUND
            return LetsplayResult(game_id, changed)

        top = candidates[0]
        if current and current.video_id == top.video_id and current.transcript:
            # Ролик тот же и уже расшифрован: обновляем просмотры, а нужно ли новое заключение
            # (сменились промпт или модель), решит letsplay_conclude
            async with self.session_factory() as session, session.begin():
                values = {"views": top.views, "checked_at": func.now()}
                await upsert_letsplay(session, game_id, values)
                await enqueue_conclusion(session, game_id, run_id)
            return LetsplayResult(game_id, changed=False)

        candidate, transcript = await self._first_with_speech(title, candidates)
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
            # Заключение относится к прежнему ролику: новое напишет letsplay_conclude
            "conclusion": None,
            "verdict": None,
            "model": None,
            "prompt_version": None,
            "input_tokens": None,
            "output_tokens": None,
            "status": LetsplayStatus.TRANSCRIBED,
            "error": None,
            "checked_at": func.now(),
        }
        async with self.session_factory() as session, session.begin():
            await upsert_letsplay(session, game_id, values)
            await enqueue_conclusion(session, game_id, run_id)
        logger.info(
            "летсплей {}: {} ({} просмотров), расшифровка {}: {} символов",
            title,
            candidate.url,
            candidate.views,
            transcript.source,
            len(transcript.text),
        )
        return LetsplayResult(game_id, changed=True)


class LetsplayConclude:
    """Задача letsplay_conclude: заключение LLM по сохранённой расшифровке"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        summarizer: Summarizer,
        max_transcript_chars: int,
    ) -> None:
        self.session_factory = session_factory
        self.summarizer = summarizer
        self.max_chars = max_transcript_chars

    async def run(self, game_id: int) -> LetsplayResult:
        async with self.session_factory() as session:
            title = await session.scalar(select(Game.title).where(Game.id == game_id))
            row = await session.scalar(select(Letsplay).where(Letsplay.game_id == game_id))
        if title is None or row is None or not row.transcript:
            raise PermanentError(f"нет сохранённой расшифровки летсплея игры {game_id}")
        up_to_date = (
            row.status == LetsplayStatus.DONE
            and row.conclusion
            and row.prompt_version == LETSPLAY_PROMPT_VERSION
            and row.model == self.summarizer.model
        )
        if up_to_date:
            return LetsplayResult(game_id, changed=False)

        conclusion = await self.summarizer.conclude(
            title, row.title or "", row.transcript[: self.max_chars]
        )
        values = {
            "conclusion": conclusion.digest.conclusion.strip(),
            "verdict": conclusion.digest.verdict,
            "model": self.summarizer.model,
            "prompt_version": LETSPLAY_PROMPT_VERSION,
            "input_tokens": conclusion.input_tokens,
            "output_tokens": conclusion.output_tokens,
            "status": LetsplayStatus.DONE,
            "error": None,
        }
        async with self.session_factory() as session, session.begin():
            await upsert_letsplay(session, game_id, values)
        return LetsplayResult(game_id, changed=True)
