"""Классификация ошибок задач: повторить позже или сразу dead"""

import sys
from dataclasses import dataclass

import pydantic

from app.core.errors import PermanentError, RetryableError

# Библиотеки воркера не импортируются: исключение из SDK значит, что SDK уже загружен.
# Так очередь работает и в образе web, где этих библиотек нет
LLM_SDKS = ("anthropic", "openai")


@dataclass(frozen=True)
class Classified:
    retryable: bool
    reason: str


def _status_retryable(status: int) -> bool:
    return status == 429 or status >= 500


def classify(exc: BaseException) -> Classified:
    """Повторяемые: сеть, таймауты, 429 и 5xx. Окончательные: 4xx, валидация, отказ модели"""
    name = type(exc).__name__
    if isinstance(exc, PermanentError):
        return Classified(False, name)
    if isinstance(exc, RetryableError):
        return Classified(True, name)
    if isinstance(exc, pydantic.ValidationError):
        return Classified(False, name)
    if isinstance(exc, TimeoutError):
        return Classified(True, name)
    if httpx := sys.modules.get("httpx"):
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return Classified(_status_retryable(status), f"HTTP {status}")
        if isinstance(exc, httpx.TransportError):
            return Classified(True, name)
    for sdk in filter(None, map(sys.modules.get, LLM_SDKS)):
        if isinstance(exc, sdk.APIConnectionError | sdk.RateLimitError):
            return Classified(True, name)
        if isinstance(exc, sdk.APIStatusError):
            return Classified(_status_retryable(exc.status_code), f"{name} {exc.status_code}")
    # Неизвестную ошибку повторяем: число попыток всё равно ограничено max_attempts
    return Classified(True, name)
