"""Настройки бота, которые задаются из Telegram и переживают перезапуск.

Пока это пароль по умолчанию для новых аккаунтов и задача по умолчанию.

Зачем пароль отдельной настройкой. При автозамене забаненного аккаунта
новый создавался с паролем старого, а при массовой регистрации пароль
вводился руками каждый раз. Хранить его в одном месте — и в реестре не
плодятся разные пароли, и в accounts.json не уезжает пустая строка, из-за
которой add_bot вообще отказывался создавать аккаунт.

Файл: bot_settings.json рядом с accounts.json.
"""

from __future__ import annotations

import json
import os
import threading

SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bot_settings.json",
)

# Задача, с которой создаётся новый аккаунт, если ничего не выбрано.
DEFAULT_TASK = "lvl"

# Задачи, которые вообще имеет смысл ставить новому аккаунту.
KNOWN_TASKS = ("lvl", "quest", "mine", "factory", "bus", "farm", "stop")

# Пароль по умолчанию. Он же подставляется в мастер массовой регистрации,
# если человек не ввёл свой.
DEFAULT_PASSWORD = "55337q"


class Settings:
    """Простое хранилище настроек в JSON с сохранением на диск."""

    def __init__(self, path: str | None = None, *, log=None):
        self.path = path or SETTINGS_FILE
        self.log = log or (lambda _m: None)
        self._lock = threading.RLock()
        self._data: dict = {
            "base_password": DEFAULT_PASSWORD,
            "default_task": DEFAULT_TASK,
            # Сколько аккаунтов разрешено создавать одной партией.
            "batch_limit": 50,
        }
        self.load()

    # --- чтение/запись ---

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self.log(f"[НАСТРОЙКИ] не прочитал {self.path}: {exc}")
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            for key in ("base_password", "default_task", "batch_limit"):
                if key in data:
                    self._data[key] = data[key]

    def save(self) -> None:
        with self._lock:
            payload = dict(self._data)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            self.log(f"[НАСТРОЙКИ] не сохранил {self.path}: {exc}")

    # --- поля ---

    @property
    def base_password(self) -> str:
        with self._lock:
            return str(self._data.get("base_password") or "").strip()

    @base_password.setter
    def base_password(self, value: str) -> None:
        value = (value or "").strip()
        with self._lock:
            self._data["base_password"] = value
        self.save()

    @property
    def default_task(self) -> str:
        with self._lock:
            task = str(self._data.get("default_task") or DEFAULT_TASK).strip().lower()
        return task if task in KNOWN_TASKS else DEFAULT_TASK

    @default_task.setter
    def default_task(self, value: str) -> None:
        value = (value or "").strip().lower()
        if value not in KNOWN_TASKS:
            value = DEFAULT_TASK
        with self._lock:
            self._data["default_task"] = value
        self.save()

    @property
    def batch_limit(self) -> int:
        with self._lock:
            try:
                return max(1, int(self._data.get("batch_limit") or 50))
            except (TypeError, ValueError):
                return 50

    @batch_limit.setter
    def batch_limit(self, value) -> None:
        try:
            number = max(1, min(500, int(value)))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._data["batch_limit"] = number
        self.save()

    # --- удобства ---

    def password_or(self, fallback: str = "") -> str:
        """Пароль для нового аккаунта: базовый, иначе переданный, иначе дефолт."""
        return self.base_password or (fallback or "").strip() or DEFAULT_PASSWORD

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)


# Один экземпляр на процесс: настройки читают и менеджер, и Telegram.
_settings: Settings | None = None
_settings_lock = threading.Lock()


def get_settings(path: str | None = None, *, log=None) -> Settings:
    global _settings
    with _settings_lock:
        if _settings is None:
            _settings = Settings(path, log=log)
        return _settings


def reset_settings() -> None:
    """Сбросить синглтон (тесты)."""
    global _settings
    with _settings_lock:
        _settings = None
