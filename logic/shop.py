"""Ларёк — покупка еды в квестах Дяди Славы.

Интерфейс приходит JSON-ом (iid=3). Ответ серверу:

    {"t": 1, "r": <id товара>}

Товар можно брать любой — в квесте важно только факт покупки. Поэтому
выбор по умолчанию: самый дешёвый из доступных, а не первый попавшийся,
чтобы не спустить деньги на дорогое.
"""

from __future__ import annotations

import re
import time

SHOP_INTERFACE = 3

_STRIP_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~")

# Поля, в которых сервер может прислать список товаров.
LIST_KEYS = ("i", "it", "items", "s", "sh", "p", "pr", "l")

# Поля одного товара.
ID_KEYS = ("id", "i", "u", "uid")
NAME_KEYS = ("n", "nm", "name", "t", "title")
PRICE_KEYS = ("p", "pr", "price", "c", "cost")


def clean(value):
    if value is None:
        return ""
    return _STRIP_RE.sub("", str(value)).strip()


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


class ShopItem:
    def __init__(self, item_id, name="", price=None, raw=None):
        self.item_id = item_id
        self.name = name
        self.price = price
        self.raw = raw or {}

    def __repr__(self):
        price = f"{self.price}р" if self.price is not None else "?"
        return f"ShopItem(#{self.item_id} {self.name!r} {price})"


class ShopMenu:
    """Разобранный ассортимент ларька."""

    def __init__(self, items, *, raw=None):
        self.items = list(items)
        self.raw = raw or {}

    def __repr__(self):
        return f"ShopMenu({self.items})"

    @classmethod
    def from_payload(cls, payload):
        """Вытащить товары из любого разумного вида payload."""
        if not isinstance(payload, dict):
            return None

        # Иногда список лежит прямо в корне как единственный список.
        candidates = []
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                candidates.append(value)
        if not candidates:
            # Последний шанс: любой список словарей с id.
            for value in payload.values():
                if isinstance(value, list) and any(
                    isinstance(row, dict) for row in value
                ):
                    candidates.append(value)

        for rows in candidates:
            items = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                item_id = _first_int(row, ID_KEYS)
                if item_id is None:
                    continue
                items.append(
                    ShopItem(
                        item_id,
                        _first_text(row, NAME_KEYS),
                        _first_int(row, PRICE_KEYS),
                        raw=row,
                    )
                )
            if items:
                return cls(items, raw=dict(payload))

        return None

    def cheapest(self):
        priced = [item for item in self.items if item.price is not None]
        if priced:
            return min(priced, key=lambda item: item.price)
        return self.items[0] if self.items else None

    def by_name(self, *words):
        for word in words:
            needle = word.lower()
            for item in self.items:
                if needle in item.name.lower():
                    return item
        return None


class ShopRunner:
    """Покупка еды: дождаться ларька, выбрать товар, купить."""

    def __init__(self, bot, *, log=None, answer_delay=0.8):
        self.bot = bot
        self.log = log or (lambda msg: None)
        self.answer_delay = float(answer_delay)

        self.menu = None
        self.received_at = 0.0
        self.purchases = 0
        self.last_error = None

    def on_payload(self, payload):
        menu = ShopMenu.from_payload(payload)
        if menu is None:
            return False
        self.menu = menu
        self.received_at = time.time()
        self.log(
            f"[shop] ларёк открыт, товаров {len(menu.items)}: "
            f"{[i.name or i.item_id for i in menu.items][:8]}"
        )
        return True

    def buy(self, *, name=None, item_id=None):
        """Купить конкретный товар или самый дешёвый. Возвращает id или None."""
        if self.menu is None:
            self.last_error = "ларёк не открыт"
            self.log(f"[shop] {self.last_error}")
            return None

        if item_id is not None:
            chosen = next(
                (item for item in self.menu.items if item.item_id == int(item_id)), None
            )
            if chosen is None:
                self.last_error = f"товара #{item_id} нет в ларьке"
                self.log(f"[shop] {self.last_error}")
                return None
        elif name:
            chosen = self.menu.by_name(name)
            if chosen is None:
                self.last_error = f"товара {name!r} нет в ларьке"
                self.log(f"[shop] {self.last_error}")
                return None
        else:
            chosen = self.menu.cheapest()
            if chosen is None:
                self.last_error = "в ларьке пусто"
                self.log(f"[shop] {self.last_error}")
                return None

        ok = self.bot.api.setbotfood(chosen.item_id)
        self.purchases += 1
        self.menu = None
        self.log(
            f"[shop] купил {chosen.name or chosen.item_id} "
            f"(id={chosen.item_id}) -> {ok}"
        )
        return chosen.item_id

    def status(self):
        return {
            "open": self.menu is not None,
            "items": len(self.menu.items) if self.menu else 0,
            "purchases": self.purchases,
            "error": self.last_error,
        }
