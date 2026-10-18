"""Выбор цвета в интерфейсе покраски (сюжетка, Сталинград).

Схема: открывается интерфейс, в нём пункт «Покрасить», затем палитра
цветов. Нужно выбрать чёрный.

Точный id интерфейса палитры на живом сервере ещё не подтверждён, поэтому
модуль не прибит к одному iid: он распознаёт палитру по содержимому
(список вариантов с цветом) и отвечает тем же ключом, каким сервер отдал
вариант. Ответ по умолчанию — {"t": 1, "r": <id варианта>}, как у ларька;
ключ можно переопределить, когда станет видно на живых данных.
"""

from __future__ import annotations

import re
import time

_STRIP_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~")

LIST_KEYS = ("c", "cl", "colors", "col", "p", "pal", "i", "it", "items")

ID_KEYS = ("id", "i", "u", "uid", "c")
NAME_KEYS = ("n", "nm", "name", "t", "title", "l")
HEX_KEYS = ("h", "hex", "col", "color", "c")

# Имена чёрного во всех видах, какие сервер может прислать.
BLACK_WORDS = ("чёрн", "черн", "black", "угол", "смоль")

# Ключевые слова пункта «покрасить» в меню.
PAINT_WORDS = ("покрас", "цвет", "перекрас", "paint", "color")


def clean(value):
    if value is None:
        return ""
    return _STRIP_RE.sub("", str(value)).strip()


def normalize_hex(value):
    """Привести цвет к #RRGGBB или вернуть None."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"#{value & 0xFFFFFF:06X}"
    text = clean(value)
    if not text:
        return None
    if text.startswith("#"):
        text = text[1:]
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return f"#{text.upper()}"
    return None


def is_black(*values):
    """Чёрный ли это: по имени или по hex."""
    for value in values:
        text = clean(value).lower()
        if text and any(word in text for word in BLACK_WORDS):
            return True
        hexed = normalize_hex(value)
        if hexed and hexed.upper() in ("#000000", "#0A0A0A", "#111111"):
            return True
    return False


class ColorOption:
    def __init__(self, option_id, name="", hex_color=None, raw=None):
        self.option_id = option_id
        self.name = name
        self.hex = normalize_hex(hex_color) if hex_color is not None else None
        self.raw = raw or {}

    def __repr__(self):
        return f"ColorOption(#{self.option_id} {self.name!r} {self.hex})"

    def is_black(self):
        return is_black(self.name, self.hex)


def _first_int(item, keys):
    for key in keys:
        if key in item:
            try:
                return int(item[key])
            except (TypeError, ValueError):
                continue
    return None


def _first_text(item, keys):
    for key in keys:
        if key in item:
            text = clean(item[key])
            if text:
                return text
    return ""


class ColorPalette:
    def __init__(self, options, *, raw=None):
        self.options = list(options)
        self.raw = raw or {}

    def __repr__(self):
        return f"ColorPalette({self.options})"

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict):
            return None

        rows = []
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            for value in payload.values():
                if isinstance(value, list) and any(isinstance(r, dict) for r in value):
                    rows = value
                    break

        options = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            option_id = _first_int(row, ID_KEYS)
            if option_id is None:
                option_id = index
            name = _first_text(row, NAME_KEYS)
            hex_color = None
            for key in HEX_KEYS:
                if key in row:
                    hex_color = normalize_hex(row[key])
                    if hex_color:
                        break
            options.append(ColorOption(option_id, name, hex_color, raw=row))

        if not options:
            return None
        return cls(options, raw=dict(payload))

    def black(self):
        for option in self.options:
            if option.is_black():
                return option
        return None

    def by_name(self, *words):
        for word in words:
            needle = word.lower()
            for option in self.options:
                if needle in option.name.lower():
                    return option
        return None


class PaintPicker:
    """Находит пункт «покрасить», затем выбирает чёрный в палитре."""

    def __init__(self, bot, *, log=None, response_key="r", response_type=1):
        self.bot = bot
        self.log = log or (lambda msg: None)
        self.response_key = response_key
        self.response_type = response_type

        self.palette = None
        self.received_at = 0.0
        self.picked = 0
        self.last_error = None

    def on_payload(self, payload):
        palette = ColorPalette.from_payload(payload)
        if palette is None:
            return False
        self.palette = palette
        self.received_at = time.time()
        self.log(
            f"[paint] палитра, цветов {len(palette.options)}: "
            f"{[o.name or o.hex or o.option_id for o in palette.options][:10]}"
        )
        black = palette.black()
        if black is not None:
            self.log(f"[paint] чёрный найден: {black}")
        else:
            self.log("[paint] чёрного в палитре нет — проверь имена")
        return True

    def pick_black(self, *, send=None):
        """Выбрать чёрный. send — своя функция отправки, если нужен другой формат."""
        if self.palette is None:
            self.last_error = "палитра не открыта"
            self.log(f"[paint] {self.last_error}")
            return None

        option = self.palette.black()
        if option is None:
            self.last_error = "чёрного нет в палитре"
            self.log(f"[paint] {self.last_error}")
            return None

        payload = {"t": self.response_type, self.response_key: option.option_id}
        if send is not None:
            ok = send(payload)
        else:
            ok = self._send(payload)

        self.log(f"[paint] выбрал {option.name or option.hex or option.option_id} "
                 f"payload={payload} -> {ok}")

        if not ok:
            # Раньше здесь возвращался id даже при провале отправки, и вызывающий
            # код считал цвет выбранным, хотя на сервер ничего не ушло.
            self.last_error = "не удалось отправить выбор цвета"
            self.log(f"[paint] {self.last_error}")
            return None

        self.picked += 1
        self.palette = None
        return option.option_id

    def _send(self, payload):
        """Отправка через интерфейс, по которому пришла палитра."""
        session = getattr(self.bot, "session", None)
        if session is None:
            return False
        iid = getattr(self, "interface_id", None)
        if iid is None:
            self.log("[paint] iid палитры неизвестен — отправка невозможна")
            return False
        return session.send_json(iid, payload, packet_name="paint_pick")

    def status(self):
        return {
            "open": self.palette is not None,
            "colors": len(self.palette.options) if self.palette else 0,
            "picked": self.picked,
            "error": self.last_error,
        }
