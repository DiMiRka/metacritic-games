"""Проверка ссылок из внешних данных: только https и доверенные хосты"""

from collections.abc import Collection
from typing import Any
from urllib.parse import urlsplit

# Хосты взяты из реальных ответов Metacritic (tests/fixtures/composer_*.json)
IMAGE_HOSTS = frozenset({"www.metacritic.com"})
VIDEO_HOSTS = frozenset({"cdn.jwplayer.com"})
YOUTUBE_HOSTS = frozenset({"www.youtube.com"})


def safe_url(value: Any, allowed_hosts: Collection[str]) -> str | None:
    """Пропускает только https-ссылку на разрешённый хост, иначе None"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if (parts.hostname or "").lower() not in allowed_hosts:
        return None
    return value
