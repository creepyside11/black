"""Входящие звонки — в сюжетке Дяди Славы на них надо отвечать.

Сервер присылает JSON iid=65:

    {"o": 1, "t": 2, "h": "Дядя Слава", "s": "Текст", "b": "Ответить"}

Ответ — тот же интерфейс с типом звонка и номером кнопки:

    {"t": <t>, "b": <button>}

Кнопка 1 — принять. Тип звонка надо вернуть тот же, что прислал сервер,
иначе он не свяжет ответ с конкретным вызовом.
"""

from __future__ import annotations

import re
import time

CALL_INTERFACE = 65
ACCEPT_BUTTON = 1
DECLINE_BUTTON = 0

_STRIP_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~")

# Слова, по которым видно, что звонок от сюжетки, а не спам.
STORY_CALLERS = ("слав", "дядя", "сюжет", "квест")


def clean(value):
    if value is None:
        return ""
    return _STRIP_RE.sub("", str(value)).strip()


class IncomingCall:
    def __init__(self, header, text, button, notification_type, *, raw=None):
        self.header = clean(header)
        self.text = clean(text)
        self.button = clean(button)
        self.notification_type = notification_type
        self.raw = raw or {}
        self.received_at = time.time()

    def __repr__(self):
        return (
            f"IncomingCall(from={self.header!r}, type={self.notification_type}, "
            f"button={self.button!r})"
        )

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict):
            return None
        if payload.get("o") != 1:
            return None
        # t обязателен: без него ответ некуда привязать.
        if payload.get("t") is None:
            return None
        return cls(
            payload.get("h", ""),
            payload.get("s", ""),
            payload.get("b", ""),
            payload.get("t"),
            raw=dict(payload),
        )

    def is_story(self):
        blob = f"{self.header} {self.text}".lower()
        return any(word in blob for word in STORY_CALLERS)


class CallAnswerer:
    """Автоматически принимает звонки, опционально только сюжетные."""

    def __init__(self, bot, *, log=None, story_only=True, cooldown=2.0):
        self.bot = bot
        self.log = log or (lambda msg: None)
        self.story_only = bool(story_only)
        self.cooldown = float(cooldown)

        self.call = None
        self.accepted = 0
        self.skipped = 0
        self._last_answer_at = 0.0

    def on_payload(self, payload):
        """Пришёл звонок. Возвращает True, если ответили."""
        call = IncomingCall.from_payload(payload)
        if call is None:
            return False

        self.call = call
        self.log(
            f"[call] входящий от {call.header or '?'!r} "
            f"(type={call.notification_type}): {call.text[:60]!r}"
        )

        if self.story_only and not call.is_story():
            self.skipped += 1
            self.log("[call] не сюжетный — пропускаю")
            return False

        now = time.time()
        if now - self._last_answer_at < self.cooldown:
            self.log("[call] слишком часто — пропускаю, чтобы не спамить")
            return False

        ok = self.bot.api.send_call_notification_response(
            notification_type=int(call.notification_type),
            button=ACCEPT_BUTTON,
        )
        self._last_answer_at = now
        self.accepted += 1
        self.log(f"[call] принял -> {ok}")
        return True

    def decline(self):
        if self.call is None:
            return None
        ok = self.bot.api.send_call_notification_response(
            notification_type=int(self.call.notification_type),
            button=DECLINE_BUTTON,
        )
        self.log(f"[call] отклонил -> {ok}")
        return ok

    def status(self):
        return {
            "accepted": self.accepted,
            "skipped": self.skipped,
            "last": self.call.header if self.call else None,
        }
