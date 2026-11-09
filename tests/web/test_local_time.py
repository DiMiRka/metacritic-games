"""Время на страницах: момент в UTC в <time datetime>, запасной текст в поясе TIMEZONE,
static/time.js переписывает его в поясе браузера"""

from datetime import UTC, datetime, timedelta, timezone

from app.core.config import Settings
from app.web.templating import build_templates


def local_time(timezone_name: str = "Europe/Moscow"):
    return build_templates(Settings(timezone=timezone_name)).env.filters["local_time"]


def test_time_tag_has_utc_moment_and_server_fallback() -> None:
    moment = datetime(2026, 11, 9, 6, 57, 40, tzinfo=UTC)
    assert local_time()(moment) == (
        '<time datetime="2026-11-09T06:57:40+00:00" data-format="datetime">09.11 09:57:40</time>'
    )
    assert local_time()(moment, "time") == (
        '<time datetime="2026-11-09T06:57:40+00:00" data-format="time">09:57</time>'
    )


def test_moment_normalized_to_utc() -> None:
    plus_five = timezone(timedelta(hours=5))
    tag = local_time("UTC")(datetime(2026, 11, 9, 11, 0, tzinfo=plus_five))
    assert 'datetime="2026-11-09T06:00:00+00:00"' in tag
    assert ">09.11 06:00:00<" in tag


def test_missing_time_is_dash() -> None:
    assert local_time()(None) == "—"
