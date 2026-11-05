import anthropic
import httpx
import openai
import pydantic
import pytest

from app.core.errors import PermanentError, RetryableError
from app.crawler.processor import GameNotFoundError
from app.enrichment.llm import SummaryError
from app.jobs.errors import classify

REQUEST = httpx.Request("GET", "https://example.test/")


def http_error(status: int) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "x", request=REQUEST, response=httpx.Response(status, request=REQUEST)
    )


def sdk_error(module, cls, status: int):
    return getattr(module, cls)("x", response=httpx.Response(status, request=REQUEST), body=None)


class Model(pydantic.BaseModel):
    n: int


def validation_error() -> pydantic.ValidationError:
    try:
        Model.model_validate({"n": "nope"})
    except pydantic.ValidationError as exc:
        return exc
    raise AssertionError


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("boom", request=REQUEST),
        httpx.ReadTimeout("slow", request=REQUEST),
        TimeoutError(),
        http_error(429),
        http_error(500),
        http_error(503),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APITimeoutError(request=REQUEST),
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        sdk_error(anthropic, "RateLimitError", 429),
        sdk_error(openai, "RateLimitError", 429),
        sdk_error(anthropic, "InternalServerError", 529),
        RetryableError("later"),
        RuntimeError("unknown"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_retryable(exc) -> None:
    assert classify(exc).retryable is True


@pytest.mark.parametrize(
    "exc",
    [
        GameNotFoundError("nope"),
        SummaryError("stop_reason=refusal"),
        validation_error(),
        http_error(404),
        http_error(403),
        sdk_error(anthropic, "BadRequestError", 400),
        sdk_error(anthropic, "AuthenticationError", 401),
        sdk_error(openai, "NotFoundError", 404),
        PermanentError("bad"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_permanent(exc) -> None:
    assert classify(exc).retryable is False


def test_reason_names_status() -> None:
    assert classify(http_error(503)).reason == "HTTP 503"
    assert classify(GameNotFoundError("x")).reason == "GameNotFoundError"
