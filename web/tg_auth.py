"""Вход в Mini App через Telegram — без пароля и без экрана логина.

Telegram открывает страницу с подписанным блобом `initData` в URL. Подпись
считается на секрете, производном от токена бота, поэтому подделать её,
не зная токена, нельзя. Панель проверяет подпись и пускает только тех, кто
в списке админов: иначе любой, кто открыл ссылку, получил бы ферму.

Алгоритм официальный:
    secret = HMAC_SHA256(key="WebAppData", msg=bot_token)
    hash   = HMAC_SHA256(key=secret, msg=data_check_string).hexdigest()
где data_check_string — отсортированные "ключ=значение" без поля hash,
склеенные переводом строки.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

# Свежесть блоба: сутки. Блоб с вечным сроком превратился бы в вечный
# пропуск, если утечёт из истории чата.
MAX_AGE = 86400

# Куда initData может приехать помимо тела запроса.
HEADER = "X-Telegram-Init-Data"

_ENV_CACHE: dict[str, str] | None = None


def _env_file_candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    return [
        os.path.join(root, ".env"),
        os.path.join(here, ".env"),
    ]


def load_env() -> dict[str, str]:
    """Настройки из окружения, с добором из .env рядом с проектом."""
    global _ENV_CACHE
    if _ENV_CACHE is not None:
        return _ENV_CACHE

    values: dict[str, str] = {}
    for path in _env_file_candidates():
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip().strip("'\"")
        except OSError:
            continue

    # Окружение сильнее файла: systemd подставляет свои значения.
    for key in ("TG_BOT_TOKEN", "TG_ADMIN_IDS", "BR_MINIAPP_URL"):
        if os.environ.get(key):
            values[key] = os.environ[key]

    _ENV_CACHE = values
    return values


def bot_token() -> str:
    return str(load_env().get("TG_BOT_TOKEN") or "")


def admin_ids() -> set[int]:
    """Кому разрешено открывать Mini App.

    Пустой список = не пускать никого. Открытая дверь по умолчанию — это
    не «удобно», это чужая ферма.
    """
    raw = str(load_env().get("TG_ADMIN_IDS") or "")
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


def miniapp_url() -> str:
    return str(load_env().get("BR_MINIAPP_URL") or "")


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs))


def validate(init_data: str, token: str, *, max_age: int = MAX_AGE) -> dict | None:
    """Проверить подпись. Возвращает разобранные данные или None."""
    if not init_data or not token:
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None

    received = ""
    rest: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "hash":
            received = value
        else:
            rest.append((key, value))

    if not received:
        return None

    secret = hmac.new(b"WebAppData", token.encode("utf-8"),
                      hashlib.sha256).digest()
    expected = hmac.new(secret, _data_check_string(rest).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received):
        return None

    payload = dict(rest)

    try:
        auth_date = int(payload.get("auth_date") or 0)
    except (TypeError, ValueError):
        return None

    if max_age > 0 and (time.time() - auth_date) > max_age:
        return None

    user = None
    if payload.get("user"):
        try:
            user = json.loads(payload["user"])
        except (ValueError, TypeError):
            return None

    return {
        "auth_date": auth_date,
        "user": user or {},
        "query_id": payload.get("query_id"),
        "start_param": payload.get("start_param"),
    }


def identify(init_data: str) -> dict | None:
    """Полный вход: подпись верна и человек в списке админов."""
    token = bot_token()
    if not token:
        return None
    parsed = validate(init_data, token)
    if parsed is None:
        return None

    user = parsed.get("user") or {}
    try:
        user_id = int(user.get("id") or 0)
    except (TypeError, ValueError):
        return None

    allowed = admin_ids()
    if not allowed or user_id not in allowed:
        return None

    return {
        "id": user_id,
        "username": str(user.get("username") or ""),
        "first_name": str(user.get("first_name") or ""),
        "last_name": str(user.get("last_name") or ""),
        "language": str(user.get("language_code") or ""),
        "auth_date": parsed["auth_date"],
    }
