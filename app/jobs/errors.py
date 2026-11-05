"""Классификация ошибок задач: повторить позже или сразу dead"""

from dataclasses import dataclass

import anthropic
import httpx
import openai
import pydantic

from app.core.errors import PermanentError, RetryableError

_SDK_STATUS_ERRORS = (anthropic.APIStatusError, openai.APIStatusError)
_SDK_TRANSIENT_ERRORS = (
    anthropic.APIConnectionError,
    openai.APIConnectionError,
    anthropic.RateLimitError,
    openai.RateLimitError,
)


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
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return Classified(_status_retryable(status), f"HTTP {status}")
    if isinstance(exc, httpx.TransportError | TimeoutError):
        return Classified(True, name)
    if isinstance(exc, _SDK_TRANSIENT_ERRORS):
        return Classified(True, name)
    if isinstance(exc, _SDK_STATUS_ERRORS):
        return Classified(_status_retryable(exc.status_code), f"{name} {exc.status_code}")
    # Неизвестную ошибку повторяем: число попыток всё равно ограничено max_attempts
    return Classified(True, name)
