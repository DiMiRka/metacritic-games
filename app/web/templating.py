from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.config import Settings
from app.core.urls import IMAGE_HOSTS, VIDEO_HOSTS, YOUTUBE_HOSTS, safe_url

WEB_DIR = Path(__file__).parent
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]  # fmt: skip
URL_HOSTS = {"image": IMAGE_HOSTS, "video": VIDEO_HOSTS, "youtube": YOUTUBE_HOSTS}


def ru_date(value: date | None) -> str:
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}" if value else "дата неизвестна"


def score_level(value: float | None, scale: int = 100) -> str:
    if value is None:
        return "none"
    value = value * 100 / scale
    return "good" if value >= 75 else "mixed" if value >= 50 else "bad"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин {secs} с" if minutes else f"{secs} с"


def safe_url_filter(value: str | None, kind: str) -> str | None:
    """Повторная проверка перед выводом: в базе могут лежать старые значения"""
    return safe_url(value, URL_HOSTS[kind])


def auth_context(request: Request) -> dict[str, Any]:
    auth = getattr(request.state, "auth", None)
    return {
        "auth": auth,
        "csrf_token": auth.csrf_token if auth else "",
        "show_docs": not request.app.state.settings.is_prod,
    }


def build_templates(settings: Settings) -> Jinja2Templates:
    tz = ZoneInfo(settings.timezone)

    def local_time(value: datetime | None) -> str:
        return value.astimezone(tz).strftime("%d.%m %H:%M:%S") if value else "—"

    templates = Jinja2Templates(directory=WEB_DIR / "templates", context_processors=[auth_context])
    templates.env.filters["ru_date"] = ru_date
    templates.env.filters["safe_url"] = safe_url_filter
    templates.env.filters["local_time"] = local_time
    templates.env.filters["duration"] = duration
    templates.env.globals["score_level"] = score_level
    return templates


def templates_of(request: Request) -> Jinja2Templates:
    return request.app.state.templates
