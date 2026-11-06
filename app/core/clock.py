"""Время сервиса: сутки обхода и время в интерфейсе считаются в TIMEZONE.
Веб передаёт часовой пояс из своих настроек, воркер и CLI берут общие
"""

from datetime import date, datetime, time, tzinfo

from app.core.config import settings


def now_tz(tz: tzinfo | None = None) -> datetime:
    return datetime.now(tz or settings.tz)


def day_bounds(now: datetime, tz: tzinfo | None = None) -> tuple[date, datetime]:
    zone = tz or settings.tz
    today = now.astimezone(zone).date()
    return today, datetime.combine(today, time.min, tzinfo=zone)
