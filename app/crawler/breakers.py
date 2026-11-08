"""Какие ошибки открывают автоматы metacritic и llm и к каким задачам они относятся"""

from datetime import datetime

import anthropic
import httpx
import openai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.crawler.processor import ScrapeFormatError
from app.jobs.breaker import BreakerPolicy, Verdict, gate, record
from app.jobs.models import JobKind
from app.jobs.queue import ClaimedJob
from app.youtube.errors import YouTubeApiConfigError, YouTubeApiError, YouTubeBlockedError
from app.youtube.search import QuotaExceededError

_AUTH_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
)
_RATE_LIMITS = (anthropic.RateLimitError, openai.RateLimitError)
_QUOTA_MARKERS = ("quota", "insufficient_quota", "resource_exhausted", "credit balance")
_TRANSPORT = (
    httpx.TransportError,
    anthropic.APIConnectionError,
    openai.APIConnectionError,
    TimeoutError,
)


def judge_metacritic(error: BaseException | None) -> Verdict:
    """Сбой Metacritic: формат ответа поменялся или 5xx и 429 после повторов клиента"""
    if error is None:
        return Verdict.SUCCESS
    if isinstance(error, ScrapeFormatError):
        return Verdict.FAILURE
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return Verdict.FAILURE if status == 429 or status >= 500 else Verdict.SUCCESS
    if isinstance(error, _TRANSPORT):
        return Verdict.NEUTRAL
    # Игры нет и другие ошибки конкретной игры: сам сервис ответил нормально
    return Verdict.SUCCESS


def judge_llm(error: BaseException | None) -> Verdict:
    """Неверный ключ и кончившаяся квота открывают сразу, серия 429 — по порогу"""
    if error is None:
        return Verdict.SUCCESS
    if isinstance(error, _AUTH_ERRORS):
        return Verdict.TRIP
    if isinstance(error, _RATE_LIMITS):
        text = str(error).lower()
        return Verdict.TRIP if any(m in text for m in _QUOTA_MARKERS) else Verdict.FAILURE
    if isinstance(error, _TRANSPORT):
        return Verdict.NEUTRAL
    return Verdict.SUCCESS


def judge_youtube(error: BaseException | None) -> Verdict:
    """Проверка «не бот ли вы», непринятый ключ и исчерпанная квота API открывают сразу:
    следующие запросы ждёт то же самое, пока не вмешается человек или не сбросится квота"""
    if error is None:
        return Verdict.SUCCESS
    if isinstance(error, YouTubeBlockedError | YouTubeApiConfigError | QuotaExceededError):
        return Verdict.TRIP
    if isinstance(error, YouTubeApiError):
        return Verdict.FAILURE if error.status == 429 or error.status >= 500 else Verdict.SUCCESS
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return Verdict.FAILURE if status == 429 or status >= 500 else Verdict.SUCCESS
    if isinstance(error, _TRANSPORT):
        return Verdict.NEUTRAL
    # Ролик недоступен, нет речи: сам YouTube ответил нормально
    return Verdict.SUCCESS


METACRITIC = BreakerPolicy(
    "metacritic",
    threshold=lambda: settings.breaker_metacritic_threshold,
    open_seconds=lambda: settings.breaker_metacritic_open_seconds,
    probe_seconds=lambda: settings.breaker_probe_seconds,
    judge=judge_metacritic,
)
LLM = BreakerPolicy(
    "llm",
    threshold=lambda: settings.breaker_llm_threshold,
    open_seconds=lambda: settings.breaker_llm_open_seconds,
    probe_seconds=lambda: settings.breaker_probe_seconds,
    judge=judge_llm,
)
YOUTUBE = BreakerPolicy(
    "youtube",
    threshold=lambda: settings.breaker_metacritic_threshold,
    # Блокировку по IP YouTube снимает не сразу: ждём столько же, сколько LLM
    open_seconds=lambda: settings.breaker_llm_open_seconds,
    probe_seconds=lambda: settings.breaker_probe_seconds,
    judge=judge_youtube,
)
# Сбой LLM не должен влиять на scrape_game и embed, поэтому у embed автомата нет
POLICY_BY_KIND = {
    JobKind.COLLECT: METACRITIC,
    JobKind.SCRAPE_GAME: METACRITIC,
    JobKind.SUMMARIZE: LLM,
    JobKind.LETSPLAY: YOUTUBE,
    # Заключение по летсплею делает LLM: кончилась квота — ждёт вместе с резюме отзывов
    JobKind.LETSPLAY_CONCLUDE: LLM,
    JobKind.TRANSLATE: LLM,
}


class Breakers:
    """Подключается к пулам задач: gate перед выполнением, outcome после"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def gate(self, job: ClaimedJob) -> datetime | None:
        policy = POLICY_BY_KIND.get(job.kind)
        return await gate(self.session_factory, policy) if policy else None

    async def outcome(self, job: ClaimedJob, error: BaseException | None) -> None:
        if policy := POLICY_BY_KIND.get(job.kind):
            await record(self.session_factory, policy, policy.judge(error))
