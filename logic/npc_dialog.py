"""Диалоги с персонажем (NPC) — это не SA-MP меню.

Разница принципиальная:

  * Меню (RPC 347) — таблица пунктов, ответ идёт RPC-ом dialog_response
    с номером строки.
  * Диалог с персонажем (JSON iid=63) — реплика НПС и кнопки внизу справа
    («Далее», «Ок», «Взять»). Ответ идёт интерфейсом NPC_DIALOG с ключом
    кнопки: {"bk": <key>}.

Сервер присылает:

    {"o": 1, "n": "Дядя Слава", "d": "Текст реплики",
     "b": [{"bn": "Далее", "bk": 1, "bt": 0}, ...]}

Задача модуля: держать текущую реплику, выбирать нужную кнопку по смыслу,
не зацикливаться на одной и той же странице и понимать, когда разговор
закончился.
"""

from __future__ import annotations

import re
import time

# Кнопки по приоритету: чем раньше в списке, тем охотнее жмём.
# «Далее» первая — в сюжетке почти всегда жмётся именно она.
BUTTON_PRIORITY = (
    ("далее", 100),
    ("продолжить", 95),
    ("ок", 90),
    ("хорошо", 88),
    ("понял", 86),
    ("готов", 84),
    ("да", 80),
    ("взять", 78),
    ("забрать", 76),
    ("согласен", 74),
    ("купить", 70),
    ("оплатить", 68),
    ("завершить", 66),
    ("сдать", 64),
    ("ответить", 62),
    ("принять", 60),
)

# Эти кнопки закрывают разговор — их жмём только если больше нечего.
CLOSING_WORDS = ("отказаться", "нет", "закрыть", "выйти", "отмена", "уйти")

_STRIP_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~")


def clean(value) -> str:
    if value is None:
        return ""
    return _STRIP_RE.sub("", str(value)).strip()


class NpcDialog:
    """Одна реплика НПС."""

    def __init__(self, name, text, buttons, *, raw=None, received_at=None):
        self.name = clean(name)
        self.text = clean(text)
        self.buttons = list(buttons or [])
        self.raw = raw or {}
        self.received_at = received_at if received_at is not None else time.time()

    def __repr__(self):
        return (
            f"NpcDialog(name={self.name!r}, text={self.text[:40]!r}, "
            f"buttons={[b.get('text') for b in self.buttons]})"
        )

    @classmethod
    def from_payload(cls, payload):
        """Собрать реплику из JSON интерфейса NPC_DIALOG."""
        if not isinstance(payload, dict):
            return None
        if payload.get("o") != 1 and payload.get("t") != 1:
            return None

        buttons = []
        for index, raw in enumerate(payload.get("b") or []):
            if not isinstance(raw, dict):
                continue
            text = clean(raw.get("bn"))
            key = raw.get("bk")
            if key is None:
                key = index
            try:
                key = int(key)
            except (TypeError, ValueError):
                key = index
            buttons.append(
                {
                    "index": index,
                    "text": text,
                    "key": key,
                    "type": raw.get("bt"),
                    "raw": raw,
                }
            )

        return cls(payload.get("n", ""), payload.get("d", ""), buttons, raw=dict(payload))

    def button_by_text(self, *words):
        """Кнопка, в тексте которой есть любое из слов."""
        for word in words:
            needle = word.lower()
            for button in self.buttons:
                if needle in button["text"].lower():
                    return button
        return None

    def best_button(self):
        """Выбрать кнопку по смыслу. Возвращает None, если жать нечего."""
        if not self.buttons:
            return None

        scored = []
        for button in self.buttons:
            label = button["text"].lower()
            score = 0
            for word, weight in BUTTON_PRIORITY:
                if word in label:
                    score = max(score, weight)
            if any(word in label for word in CLOSING_WORDS):
                score -= 200
            # Кнопка без текста — крайний случай, но лучше, чем ничего.
            if not label:
                score -= 50
            scored.append((score, -button["index"], button))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score, _, best = scored[0]
        if best_score <= -100:
            return None
        return best


class NpcDialogFlow:
    """Ведёт разговор с НПС, листая реплики кнопкой «Далее»."""

    def __init__(
        self,
        bot,
        *,
        log=None,
        answer_delay=0.6,
        max_pages=40,
        on_page=None,
    ):
        self.bot = bot
        self.log = log or (lambda msg: None)
        self.answer_delay = float(answer_delay)
        self.max_pages = int(max_pages)
        self.on_page = on_page

        self.dialog = None
        self.pages = 0
        self.answered = 0
        self.closed = False
        self._last_sent = None
        self._last_sent_at = 0.0
        self._seen = []

    # ---- события -----------------------------------------------------

    def on_payload(self, payload):
        """Пришла реплика. Возвращает True, если разговор продолжается."""
        dialog = NpcDialog.from_payload(payload)
        if dialog is None:
            return False

        self.dialog = dialog
        self.pages += 1
        self.closed = False
        self._seen.append(dialog.text[:60])
        self.log(
            f"[npc] {dialog.name or 'НПС'}: {dialog.text[:80]!r} "
            f"| кнопки {[b['text'] for b in dialog.buttons]}"
        )

        if self.on_page is not None:
            try:
                self.on_page(dialog)
            except Exception as exc:
                self.log(f"[npc] on_page упал: {exc!r}")

        if self.pages > self.max_pages:
            self.log(f"[npc] больше {self.max_pages} реплик — стоп, похоже на цикл")
            self.closed = True
            return False

        return True

    def on_close(self):
        """Сервер закрыл интерфейс диалога."""
        self.dialog = None
        self.closed = True
        self.log(f"[npc] разговор закрыт, реплик пройдено: {self.pages}")

    # ---- действия ----------------------------------------------------

    def advance(self):
        """Нажать «Далее» (или лучшую кнопку). Возвращает отправленный ключ."""
        if self.dialog is None:
            return None

        button = self.dialog.best_button()
        if button is None:
            self.log("[npc] жать нечего — все кнопки закрывающие")
            return None

        now = time.time()
        if (
            self._last_sent == button["key"]
            and now - self._last_sent_at < self.answer_delay
        ):
            # Не спамим одним и тем же ключом чаще, чем сервер успевает
            # ответить, иначе получаем дубли реплик.
            return None

        ok = self.bot.api.send_npc_dialog_response(button["key"])
        self._last_sent = button["key"]
        self._last_sent_at = now
        self.answered += 1
        self.log(
            f"[npc] нажал {button['text']!r} (key={button['key']}) -> {ok}"
        )
        return button["key"]

    def choose(self, *words):
        """Нажать конкретную кнопку по тексту. None, если такой нет."""
        if self.dialog is None:
            return None
        button = self.dialog.button_by_text(*words)
        if button is None:
            self.log(f"[npc] кнопки {words} нет среди {[b['text'] for b in self.dialog.buttons]}")
            return None
        ok = self.bot.api.send_npc_dialog_response(button["key"])
        self.answered += 1
        self.log(f"[npc] выбрал {button['text']!r} (key={button['key']}) -> {ok}")
        return button["key"]

    # ---- состояние ---------------------------------------------------

    def status(self):
        return {
            "pages": self.pages,
            "answered": self.answered,
            "closed": self.closed,
            "name": self.dialog.name if self.dialog else None,
            "text": self.dialog.text[:60] if self.dialog else None,
            "buttons": [b["text"] for b in self.dialog.buttons] if self.dialog else [],
            "seen": list(self._seen[-5:]),
        }
