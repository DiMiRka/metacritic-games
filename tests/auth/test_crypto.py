import pytest

from app.auth import crypto
from app.core.config import settings

OLD, NEW = "o" * 64, "n" * 64


def test_roundtrip_hides_plaintext_and_binds_context(monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_key", NEW)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    sealed = crypto.encrypt(
        "JBSWY3DPEHPK3PXP", purpose="totp", context="user:1", secrets=settings.secret_keys
    )

    assert sealed.startswith("v1.")
    assert "JBSWY3DPEHPK3PXP" not in sealed
    assert (
        crypto.decrypt(sealed, purpose="totp", context="user:1", secrets=settings.secret_keys)
        == "JBSWY3DPEHPK3PXP"
    )
    # Тот же текст шифруется каждый раз по-новому: nonce случайный
    assert sealed != crypto.encrypt(
        "JBSWY3DPEHPK3PXP", purpose="totp", context="user:1", secrets=settings.secret_keys
    )
    with pytest.raises(crypto.SecretDecryptError, match="контекста"):
        crypto.decrypt(sealed, purpose="totp", context="user:2", secrets=settings.secret_keys)
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt(sealed, purpose="other", context="user:1", secrets=settings.secret_keys)


def test_rotation_with_previous_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_key", OLD)
    monkeypatch.setattr(settings, "secret_key_previous", "")
    sealed = crypto.encrypt(
        "secret", purpose="totp", context="user:1", secrets=settings.secret_keys
    )

    monkeypatch.setattr(settings, "secret_key", NEW)
    with pytest.raises(crypto.SecretDecryptError, match="SECRET_KEY_PREVIOUS"):
        crypto.decrypt(sealed, purpose="totp", context="user:1", secrets=settings.secret_keys)

    monkeypatch.setattr(settings, "secret_key_previous", f" {OLD} , ")
    assert (
        crypto.decrypt(sealed, purpose="totp", context="user:1", secrets=settings.secret_keys)
        == "secret"
    )
    assert crypto.needs_rekey(sealed, purpose="totp", secrets=settings.secret_keys)
    fresh = crypto.encrypt("secret", purpose="totp", context="user:1", secrets=settings.secret_keys)
    assert not crypto.needs_rekey(fresh, purpose="totp", secrets=settings.secret_keys)
    assert crypto.needs_rekey("PLAINTEXTBASE32", purpose="totp", secrets=settings.secret_keys)


def test_tampered_value_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "secret_key", NEW)
    sealed = crypto.encrypt(
        "secret", purpose="totp", context="user:1", secrets=settings.secret_keys
    )
    head, payload = sealed.rsplit(".", 1)
    broken = head + "." + ("A" if payload[0] != "A" else "B") + payload[1:]
    with pytest.raises(crypto.SecretDecryptError):
        crypto.decrypt(broken, purpose="totp", context="user:1", secrets=settings.secret_keys)
    with pytest.raises(crypto.SecretDecryptError, match="формат"):
        crypto.decrypt("garbage", purpose="totp", context="user:1", secrets=settings.secret_keys)
