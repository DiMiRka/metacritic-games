import io
import json
import logging

from loguru import logger

from app.core import logs
from app.core.config import settings


def capture_json(monkeypatch) -> io.StringIO:
    out = io.StringIO()
    monkeypatch.setattr(settings, "log_format", "json")
    monkeypatch.setattr(logs.sys, "stdout", out)
    logs.configure_logging()
    return out


def lines(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_json_lines_carry_context_and_exception(monkeypatch) -> None:
    out = capture_json(monkeypatch)
    try:
        with logs.log_context(run_id=7, job_id=42, slug="elden-ring", game_id=None):
            logger.info("обработка {}", "игры")
            try:
                raise ValueError("сломалось")
            except ValueError:
                logger.exception("сбой")
        logger.info("вне контекста")
    finally:
        logs.configure_logging()

    first, failure, outside = lines(out)
    assert first["msg"] == "обработка игры"
    assert (first["run_id"], first["job_id"], first["slug"]) == (7, 42, "elden-ring")
    assert "game_id" not in first
    assert first["level"] == "INFO"
    assert "ValueError: сломалось" in failure["exception"]
    assert "run_id" not in outside


def test_standard_logging_is_intercepted(monkeypatch) -> None:
    out = capture_json(monkeypatch)
    try:
        with logs.log_context(request_id="abc"):
            logging.getLogger("uvicorn.error").warning("порт %s занят", 8000)
    finally:
        logs.configure_logging()

    (line,) = lines(out)
    assert (line["logger"], line["msg"], line["request_id"]) == (
        "uvicorn.error",
        "порт 8000 занят",
        "abc",
    )


def test_text_format_escapes_braces_and_tags(monkeypatch, capsys) -> None:
    monkeypatch.setattr(settings, "log_format", "text")
    logs.configure_logging()
    with logs.log_context(slug="<red>{x}"):
        logger.info("payload {}", "{a}")
    logs.configure_logging()
    err = capsys.readouterr().err
    assert "payload {a}" in err
    assert "slug=<red>{x}" in err


async def test_context_does_not_leak_between_tasks(log_records) -> None:
    import asyncio

    async def job(n: int) -> None:
        with logs.log_context(job_id=n):
            await asyncio.sleep(0.01)
            logger.info("job")

    await asyncio.gather(job(1), job(2))
    ours = [r for r in log_records if r["message"] == "job" and r["function"] == "job"]
    assert sorted(r["extra"]["job_id"] for r in ours) == [1, 2]
