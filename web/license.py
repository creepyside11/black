"""Вход в панель по ключу доступа — без логина и пароля.

Приложение уже проверило ключ у себя и держит подписанный токен. Панель
проверяет ту же подпись тем же публичным ключом и, если срок не вышел,
выдаёт обычную сессию. Пароль в этом пути не участвует вообще.

Токен — base64url(payload).base64url(подпись), Ed25519. Подпись считается
по тексту base64url-строки, а не по исходному JSON.
"""

from __future__ import annotations

import base64
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Публичная половина ключа подписи. Приватная лежит только в Vercel.
PUBLIC_KEY_B64 = "GwnarW5Lo6MyWVp1Z/1POZYIy7e3b0Gifyhxa6m6diI="


def _b64url(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def verify(token: str) -> dict | None:
    """Разобрать токен. Возвращает payload или None, если он не настоящий."""
    if not isinstance(token, str):
        return None
    parts = token.strip().split(".")
    if len(parts) != 2:
        return None

    encoded, signature = parts
    try:
        payload_bytes = _b64url(encoded)
        signature_bytes = _b64url(signature)
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(PUBLIC_KEY_B64)
        )
        # Подписан именно текст base64url-строки.
        key.verify(signature_bytes, encoded.encode("utf-8"))
    except (InvalidSignature, ValueError, TypeError):
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    expires = payload.get("exp")
    if not isinstance(expires, (int, float)) or expires <= time.time():
        return None

    return payload
