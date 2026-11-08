"""Вход в панель: логин и пароль, который знает только программа.

Пароль не задаётся руками — он генерируется при первом запуске панели,
показывается один раз в консоли и в Telegram владельцу, а на диск кладётся
только его scrypt-хеш. Забыл — генерируется новый, старый перестаёт
работать.

Хранилище: _web/auth.json
    {"login": "...", "salt": "...", "hash": "...", "iterations": 200000,
     "created_at": ...}

Токен сессии — не хранится на диске вообще. Живёт в памяти процесса, потому
что панель и так живёт в одном процессе: перезапуск = выход из всех сессий.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import string
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(os.path.dirname(HERE), "_web", "auth.json")

ITERATIONS = 200_000
SESSION_TTL = 12 * 3600

# Из набора убираны похожие символы: пароль будут диктовать в чат и
# переписывать руками, а 0/O и 1/l — это гарантированные опечатки.
ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "OolI")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#$%&*+-=?"
)


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_login() -> str:
    return "rak" + secrets.token_hex(3)


def _hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=bytes.fromhex(salt),
        n=2 ** 14,
        r=8,
        p=1,
        dklen=32,
    ).hex()


class Auth:
    def __init__(self, path: str = AUTH_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._sessions: dict[str, float] = {}
        self._data: dict = {}
        self._load()

    # --- хранилище ---

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception:
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def configured(self) -> bool:
        return bool(self._data.get("login") and self._data.get("hash"))

    @property
    def login(self) -> str:
        return str(self._data.get("login") or "")

    # --- выдача учётки ---

    def issue(self, login: str | None = None, password: str | None = None) -> tuple[str, str]:
        """Создать или перевыпустить учётку. Возвращает (login, password).

        Пароль в открытом виде существует только в этом возврате: на диск
        уходит хеш. Показать его второй раз будет нечем — только перевыпуск.
        """
        login = (login or generate_login()).strip()
        password = password or generate_password()
        salt = secrets.token_hex(16)
        digest = _hash(password, salt, ITERATIONS)
        with self._lock:
            self._data = {
                "login": login,
                "salt": salt,
                "hash": digest,
                "iterations": ITERATIONS,
                "created_at": time.time(),
            }
            self._save()
            # Все старые сессии недействительны: пароль сменился.
            self._sessions.clear()
        return login, password

    # --- проверка ---

    def verify(self, login: str, password: str) -> bool:
        with self._lock:
            if not self.configured:
                return False
            # Сравниваем байты, а не строки: compare_digest падает на
            # не-ASCII (а в поле логина может прилететь что угодно).
            if not hmac.compare_digest(
                login.strip().encode("utf-8"), self.login.encode("utf-8")
            ):
                # Всё равно считаем хеш: иначе по времени ответа видно,
                # существует ли логин.
                _hash(password, self._data["salt"], ITERATIONS)
                return False
            expected = self._data["hash"]
            actual = _hash(password, self._data["salt"],
                           int(self._data.get("iterations", ITERATIONS)))
            return hmac.compare_digest(expected, actual)

    def start_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + SESSION_TTL
        return token

    def check_session(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires < time.time():
                self._sessions.pop(token, None)
                return False
            return True

    def end_session(self, token: str | None) -> None:
        with self._lock:
            if token:
                self._sessions.pop(token, None)
