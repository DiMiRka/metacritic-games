"""Логи через loguru: JSON в проде, текст в разработке, контекст задачи и запроса в каждой строке.

Поля контекста (run_id, job_id, slug, request_id…) задаёт log_context: loguru хранит их в
contextvars, поэтому они видны во всех строках внутри задачи или запроса и не протекают в
соседние задачи. Стандартный logging (uvicorn, SQLAlchemy, APScheduler, httpx) перехватывается
и идёт через те же sink'и
"""

import contextlib
import json
import logging
import sys
import traceback
from collections.abc import Iterator
from typing import Any

import sentry_sdk
from loguru import logger

from app.core.config import Settings, settings

NOISY_LOGGERS = ("httpx", "httpcore", "apscheduler.executors.default", "apscheduler.scheduler")
INTERCEPTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi")
# Имя исходного логгера для строк из стандартного logging
SOURCE_KEY = "logger"


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.bind(**{SOURCE_KEY: record.name}).opt(exception=record.exc_info).log(
            level, record.getMessage()
        )


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record["extra"].items() if k != SOURCE_KEY and v is not None}


def _json_sink(message: Any) -> None:
    record = message.record
    data = {
        "ts": record["time"].isoformat(timespec="milliseconds"),
        "level": record["level"].name,
        "logger": record["extra"].get(SOURCE_KEY, record["name"]),
        "msg": record["message"],
        **_fields(record),
    }
    if record["exception"]:
        exc = record["exception"]
        data["exception"] = "".join(traceback.format_exception(exc.type, exc.value, exc.traceback))
    sys.stdout.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _escape(value: str) -> str:
    """Формат loguru — шаблон: скобки и теги цвета из данных должны остаться текстом"""
    return value.replace("{", "{{").replace("}", "}}").replace("<", r"\<")


def _text_format(record: dict[str, Any]) -> str:
    source = _escape(str(record["extra"].get(SOURCE_KEY, record["name"])))
    fields = " ".join(f"{k}={v}" for k, v in _fields(record).items())
    tail = f" <cyan>{_escape(fields)}</cyan>" if fields else ""
    return (
        "<green>{time:HH:mm:ss.SSS}</green> <level>{level: <7}</level> "
        f"{source} {{message}}{tail}\n{{exception}}"
    )


def configure_logging(config: Settings | None = None) -> None:
    """Можно вызывать повторно: sink'и и перехват ставятся заново"""
    config = config or settings
    logger.remove()
    level = config.log_level.upper()
    if config.log_format == "json":
        logger.add(_json_sink, level=level, backtrace=False, diagnose=False)
    else:
        logger.add(sys.stderr, level=level, format=_text_format, diagnose=False)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in INTERCEPTED_LOGGERS:
        std = logging.getLogger(name)
        std.handlers = [InterceptHandler()]
        std.propagate = False
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Поля во всех строках лога внутри блока; те же поля становятся тегами событий Sentry"""
    fields = {k: v for k, v in fields.items() if v is not None}
    with logger.contextualize(**fields), sentry_sdk.new_scope() as scope:
        for key, value in fields.items():
            scope.set_tag(key, str(value))
        yield
