"""Время сервиса: сутки обхода и время в интерфейсе считаются в settings.timezone"""

from datetime import date, datetime, time

from app.core.config import settings


def now_tz() -> datetime:
    return datetime.now(settings.tz)


def day_bounds(now: datetime) -> tuple[date, datetime]:
    today = now.astimezone(settings.tz).date()
    return today, datetime.combine(today, time.min, tzinfo=settings.tz)
