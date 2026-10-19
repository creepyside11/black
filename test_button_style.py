"""Цвет кнопок Telegram: поле style и допустимые значения.

Официальные значения — danger (красный), success (зелёный), primary (синий).
Проверяем, что каждая кнопка контроллера покрашена и что библиотека
действительно кладёт поле в JSON, который уходит в API.
"""

import json
import os
import re
import unittest

from telebot import types

CONTROLLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_controller.py")
ALLOWED = {"danger", "success", "primary"}


class SerializationTests(unittest.TestCase):
    def test_style_reaches_json(self):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("стоп", callback_data="x", style="danger"))
        payload = json.loads(markup.to_json())
        button = payload["inline_keyboard"][0][0]
        self.assertEqual(button["style"], "danger")

    def test_style_survives_row(self):
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("a", callback_data="a", style="success"),
            types.InlineKeyboardButton("b", callback_data="b", style="primary"),
        )
        payload = json.loads(markup.to_json())
        styles = [b["style"] for b in payload["inline_keyboard"][0]]
        self.assertEqual(styles, ["success", "primary"])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        with open(CONTROLLER, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_every_button_is_styled(self):
        total = len(re.findall(r"types\.InlineKeyboardButton\(", self.src))
        styled = len(re.findall(r"style=(?:\"|btn_style)", self.src))
        self.assertEqual(total, styled, f"без цвета осталось {total - styled}")

    def test_only_allowed_values(self):
        values = set(re.findall(r'style="(\w+)"', self.src))
        self.assertTrue(values <= ALLOWED, values - ALLOWED)

    def _style_after(self, callback):
        """Стиль кнопки с этим колбэком, даже если он на следующей строке."""
        index = self.src.find(f'callback_data=f"{callback}')
        if index < 0:
            index = self.src.find(f'callback_data="{callback}')
        self.assertGreaterEqual(index, 0, callback)
        window = self.src[index:index + 200]
        found = re.search(r'style="(\w+)"', window)
        return found.group(1) if found else None

    def test_destructive_actions_are_red(self):
        for callback in ("act_stopall", "bot_del:", "bot_stop:", "bot_proxy_reset:"):
            self.assertEqual(self._style_after(callback), "danger", callback)

    def test_start_actions_are_green(self):
        for callback in ("act_startall", "bot_start:", "act_start_some:"):
            self.assertEqual(self._style_after(callback), "success", callback)


if __name__ == "__main__":
    unittest.main(verbosity=2)
