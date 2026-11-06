import base64

from app.auth import totp

# Тестовый ключ и значения из приложения B к RFC 6238 (SHA-1), последние 6 цифр
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_rfc6238_vectors() -> None:
    for unix_time, expected in [(59, "287082"), (1111111109, "081804"), (1234567890, "005924")]:
        assert totp.code_at(RFC_SECRET, totp.current_step(unix_time)) == expected


def test_match_step_allows_drift_and_rejects_reuse() -> None:
    now = 1234567890
    step = totp.current_step(now)
    previous = totp.code_at(RFC_SECRET, step - 1)

    assert totp.match_step(RFC_SECRET, previous, last_step=None, now=now) == step - 1
    assert totp.match_step(RFC_SECRET, "005 924", last_step=None, now=now) == step
    assert totp.match_step(RFC_SECRET, "005924", last_step=step, now=now) is None
    assert (
        totp.match_step(RFC_SECRET, totp.code_at(RFC_SECRET, step - 2), last_step=None, now=now)
        is None
    )
    assert totp.match_step(RFC_SECRET, "abc", last_step=None, now=now) is None


def test_provisioning_uri() -> None:
    uri = totp.provisioning_uri("ABCDEF", "admin")
    assert uri.startswith("otpauth://totp/Metacritic%20Games%3Aadmin?secret=ABCDEF")
    assert "issuer=Metacritic+Games" in uri
