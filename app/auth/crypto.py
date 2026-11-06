"""Шифрование секретов в базе: AES-256-GCM, ключ выводится из SECRET_KEY через HKDF.

Значение хранится как v1.<id ключа>.<base64url(nonce + шифротекст)>. Id ключа — начало
sha256 производного ключа, по нему видно, каким ключом зашифровано значение. Контекст
(например, id пользователя) входит в проверку целостности: шифротекст одного пользователя
не подойдёт другому.

Смена ключа: новый SECRET_KEY, прежний в SECRET_KEY_PREVIOUS. Расшифровка пробует все
ключи, а web при старте перешифровывает значения текущим. Когда перешифровано всё,
SECRET_KEY_PREVIOUS убирают
"""

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"
NONCE_BYTES = 12


class SecretDecryptError(Exception):
    """Значение не расшифровать: нет нужного ключа или данные повреждены"""


@dataclass(frozen=True)
class _Key:
    id: str
    aead: AESGCM


def _derive(secret: str, purpose: str) -> _Key:
    raw = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"metacritic-games:{purpose}".encode(),
    ).derive(secret.encode())
    return _Key(hashlib.sha256(raw).hexdigest()[:8], AESGCM(raw))


def _keys(purpose: str, secrets: list[str]) -> list[_Key]:
    """Текущий ключ первым, за ним прежние"""
    return [_derive(secret, purpose) for secret in secrets]


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_encrypted(value: str) -> bool:
    return value.startswith(f"{VERSION}.")


def encrypt(plaintext: str, *, purpose: str, context: str, secrets: list[str]) -> str:
    key = _keys(purpose, secrets)[0]
    nonce = os.urandom(NONCE_BYTES)
    sealed = key.aead.encrypt(nonce, plaintext.encode(), context.encode())
    return f"{VERSION}.{key.id}.{_encode(nonce + sealed)}"


def decrypt(value: str, *, purpose: str, context: str, secrets: list[str]) -> str:
    try:
        version, key_id, payload = value.split(".", 2)
    except ValueError:
        raise SecretDecryptError("неизвестный формат") from None
    if version != VERSION:
        raise SecretDecryptError(f"неизвестная версия {version}")
    key = next((k for k in _keys(purpose, secrets) if k.id == key_id), None)
    if key is None:
        raise SecretDecryptError(f"нет ключа {key_id}: проверьте SECRET_KEY_PREVIOUS")
    data = _decode(payload)
    try:
        plain = key.aead.decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], context.encode())
    except InvalidTag:
        raise SecretDecryptError("данные повреждены или от другого контекста") from None
    return plain.decode()


def needs_rekey(value: str, *, purpose: str, secrets: list[str]) -> bool:
    """Открытый текст или значение под прежним ключом"""
    if not is_encrypted(value):
        return True
    return value.split(".", 2)[1] != _keys(purpose, secrets)[0].id
