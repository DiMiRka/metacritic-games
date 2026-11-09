from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.catalog.similar import (
    Closeness,
    SameDeveloper,
    SharedGenres,
    SharedPlatforms,
    SimilarDescription,
    SimilarGame,
)
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


SIMILAR_LINE_LIMIT = 60
SIMILAR_MAX_PARTS = 3
SIMILAR_MAX_GENRES = 2
# Эмбеддинг считается по названию, жанрам, разработчику и описанию: когда ни одна причина
# не прошла порог, честно сказать можно только это
SIMILAR_FALLBACK = "похожа по описанию и жанрам"
CLOSENESS_WORDS = {
    Closeness.VERY_CLOSE: "очень близкое описание",
    Closeness.CLOSE: "близкое описание",
}


def _names(names: tuple[str, ...], limit: int) -> str:
    shown = ", ".join(names[:limit])
    return f"{shown} и ещё {len(names) - limit}" if len(names) > limit else shown


def similar_reasons(item: SimilarGame) -> str:
    """Строка «почему похожа»: разработчик, жанры, близость описания, платформы только
    в добавку к единственной другой причине. Не длиннее SIMILAR_LINE_LIMIT, первая причина всегда"""
    parts: list[str] = []
    platforms = None
    for reason in item.reasons:
        if isinstance(reason, SameDeveloper):
            parts.append("тоже от " + _names(reason.names, 2))
        elif isinstance(reason, SharedGenres):
            parts.append(_names(reason.names, SIMILAR_MAX_GENRES))
        elif isinstance(reason, SimilarDescription):
            parts.append(CLOSENESS_WORDS[reason.level])
        elif isinstance(reason, SharedPlatforms):
            platforms = "есть на " + " и ".join(reason.names)
    if platforms and len(parts) == 1:
        parts.append(platforms)
    if not parts:
        return SIMILAR_FALLBACK

    line = parts[0]
    for part in parts[1:SIMILAR_MAX_PARTS]:
        if len(line) + len(" · ") + len(part) > SIMILAR_LINE_LIMIT:
            break
        line += " · " + part
    return line


def plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова после числа: 1 отзыв, 3 отзыва, 11 отзывов"""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
    templates.env.filters["plural"] = plural
    templates.env.filters["similar_reasons"] = similar_reasons
    templates.env.globals["score_level"] = score_level
    return templates


def templates_of(request: Request) -> Jinja2Templates:
    return request.app.state.templates
