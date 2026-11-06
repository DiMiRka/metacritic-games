"""Одноразовые коды TOTP по RFC 6238: SHA-1, 6 цифр, шаг 30 секунд, как в Google Authenticator,
Aegis, 1Password и других приложениях"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP_SECONDS = 30
DIGITS = 6
# Допуск на расхождение часов телефона и сервера: соседний шаг в обе стороны
DRIFT_STEPS = 1
ISSUER = "Metacritic Games"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _key(secret: str) -> bytes:
    cleaned = "".join(secret.split()).upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def match_step(
    secret: str, code: str, *, last_step: int | None, now: float | None = None
) -> int | None:
    """Шаг, которому соответствует код. Код уже использованного шага второй раз не проходит"""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    center = current_step(now)
    for step in range(center - DRIFT_STEPS, center + DRIFT_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, login: str) -> str:
    """Ссылка otpauth:// для приложения: вставить вручную или превратить в QR-код"""
    label = quote(f"{ISSUER}:{login}")
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"
