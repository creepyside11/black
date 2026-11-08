"""Тесты автосбора урожая. Без сети и без аккаунта."""
import unittest

import logic.harvester as H
from logic.menu import STYLE_LIST, STYLE_TABLIST_HEADERS


class FakeSession:
    connected = True

    def __init__(self):
        self.sent = []

    def send_dialog_response(self, dialog_id, response, list_item, text):
        self.sent.append((dialog_id, response, list_item, text))
        return True


class FakeAnswerer:
    def __init__(self):
        self.calls = []

    def answer(self, session, dialog_id, list_item=0, response=1, text="",
               *, force=False, note="", signature=None):
        self.calls.append(
            dict(dialog_id=dialog_id, list_item=list_item,
                 response=response, note=note, signature=signature)
        )
        return True


class FakeBot:
    def __init__(self, session=None, answerer=None):
        self.session = session or FakeSession()
        self.menu_answerer = answerer or FakeAnswerer()
        self.logs = []

    def add_log(self, text):
        self.logs.append(str(text))


class ParseKgTests(unittest.TestCase):
    def test_kg(self):
        self.assertEqual(H.parse_kg("Собрано 5 кг"), 5.0)

    def test_decimal_comma(self):
        self.assertEqual(H.parse_kg("2,5 кг"), 2.5)

    def test_kg_latin(self):
        self.assertEqual(H.parse_kg("12.0 kg"), 12.0)

    def test_none(self):
        self.assertIsNone(H.parse_kg("нет чисел"))
        self.assertIsNone(H.parse_kg(None))

    def test_color_tags_stripped(self):
        self.assertEqual(H.parse_kg("{FFFFFF}7 кг"), 7.0)


class RecognitionTests(unittest.TestCase):
    def test_harvest_text(self):
        self.assertTrue(H.looks_like_harvest("Сбор урожая", ""))
        self.assertTrue(H.looks_like_harvest("", "Сорвать фрукт?"))
        self.assertFalse(H.looks_like_harvest("Статистика", "Уровень: 2"))

    def test_work_menu(self):
        self.assertTrue(
            H.looks_like_work_menu("Работы", "1. Сборщик\n2. Шахтёр")
        )
        self.assertFalse(H.looks_like_work_menu("Меню игрока", "Статистика"))


class DecideTests(unittest.TestCase):
    def test_picks_harvester_in_work_menu(self):
        action = H.decide_harvest(
            STYLE_LIST, "Работы", "1. Сборщик\n2. Шахтёр"
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.kind, "pick")
        self.assertEqual(action.list_item, 0)
        self.assertEqual(action.response, 1)

    def test_work_menu_second_item(self):
        action = H.decide_harvest(
            STYLE_LIST, "Работы", "1. Шахтёр\n2. Сборщик"
        )
        self.assertEqual(action.list_item, 1)

    def test_tablist_headers_shift(self):
        # Стиль 5 — первая строка шапка, её сервер не считает.
        info = "Название\n1. Сборщик\n2. Шахтёр"
        action = H.decide_harvest(STYLE_TABLIST_HEADERS, "Работы", info)
        self.assertEqual(action.list_item, 0)

    def test_collect_button(self):
        action = H.decide_harvest(
            STYLE_LIST, "Сбор урожая", "Собрать урожай?", "Собрать", "Отмена"
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.response, 1)
        self.assertEqual(action.list_item, 0)

    def test_collect_by_row(self):
        action = H.decide_harvest(
            STYLE_LIST, "Поле", "1. Собрать\n2. Уйти"
        )
        self.assertEqual(action.kind, "pick")
        self.assertEqual(action.list_item, 0)

    def test_button_wins_over_status_text(self):
        # «Собрано 7 кг» — статус, а не пункт списка. Нажимаем кнопку.
        action = H.decide_harvest(
            STYLE_LIST, "Сбор урожая", "Собрано 7 кг", "Собрать", "Отмена"
        )
        self.assertEqual(action.kind, "button")
        self.assertEqual(action.response, 1)

    def test_list_pick_when_two_rows(self):
        action = H.decide_harvest(
            STYLE_LIST, "Поле", "1. Собрать\n2. Уйти", "", ""
        )
        self.assertEqual(action.kind, "pick")
        self.assertEqual(action.list_item, 0)

    def test_second_row_picked_correctly(self):
        action = H.decide_harvest(
            STYLE_LIST, "Поле", "1. Уйти\n2. Собрать урожай", "", ""
        )
        self.assertEqual(action.kind, "pick")
        self.assertEqual(action.list_item, 1)

    def test_unrelated_dialog_ignored(self):
        self.assertIsNone(
            H.decide_harvest(STYLE_LIST, "Статистика", "Уровень: 2")
        )

    def test_skip_needs_active(self):
        # bSkipDiag работает только когда автосбор включён.
        self.assertIsNone(
            H.decide_harvest(STYLE_LIST, "Инфо", "текст", "Продолжить", "")
        )
        action = H.decide_harvest(
            STYLE_LIST, "Инфо", "текст", "Продолжить", "", active=True
        )
        self.assertIsNotNone(action)
        self.assertEqual(action.response, 1)

    def test_never_presses_cancel(self):
        # «Отмена» не должна нажиматься никогда.
        action = H.decide_harvest(
            STYLE_LIST, "Инфо", "текст", "Отмена", "Отмена", active=True
        )
        self.assertIsNone(action)


class HarvesterTests(unittest.TestCase):
    def test_starts_inactive(self):
        self.assertFalse(H.Harvester().active)

    def test_start_stop(self):
        h = H.Harvester()
        h.start()
        self.assertTrue(h.active)
        h.stop()
        self.assertFalse(h.active)

    def test_answers_dialog(self):
        h = H.Harvester(delay=(0, 0))
        h.start()
        bot = FakeBot()
        handled = h.on_dialog(
            bot, 0, STYLE_LIST, "Работы", "1. Сборщик", "", "",
            background=False,
        )
        self.assertTrue(handled)
        self.assertEqual(len(bot.menu_answerer.calls), 1)
        call = bot.menu_answerer.calls[0]
        self.assertEqual(call["list_item"], 0)
        self.assertEqual(call["response"], 1)

    def test_ignores_unrelated(self):
        h = H.Harvester(delay=(0, 0))
        bot = FakeBot()
        self.assertFalse(
            h.on_dialog(bot, 0, STYLE_LIST, "Статистика", "Уровень: 2",
                        "", "", background=False)
        )
        self.assertEqual(bot.menu_answerer.calls, [])

    def test_dedup_same_dialog(self):
        h = H.Harvester(delay=(0, 0))
        h.start()
        bot = FakeBot()
        for _ in range(3):
            h.on_dialog(bot, 0, STYLE_LIST, "Работы", "1. Сборщик",
                        "", "", background=False)
        self.assertEqual(len(bot.menu_answerer.calls), 1)

    def test_no_session_no_answer(self):
        h = H.Harvester(delay=(0, 0))
        h.start()
        bot = FakeBot()
        bot.session.connected = False
        h.on_dialog(bot, 0, STYLE_LIST, "Работы", "1. Сборщик",
                    "", "", background=False)
        self.assertEqual(bot.menu_answerer.calls, [])

    def test_inactive_ignores_everything(self):
        # Пока автосбор не включён, ни один диалог не трогаем.
        h = H.Harvester(delay=(0, 0))
        bot = FakeBot()
        self.assertFalse(
            h.on_dialog(bot, 0, STYLE_LIST, "Работы", "1. Сборщик",
                        "", "", background=False)
        )
        self.assertEqual(bot.menu_answerer.calls, [])

    def test_env_autostart(self):
        import os
        os.environ[H.ENV_AUTOSTART] = "1"
        try:
            self.assertTrue(H.autostart_enabled())
        finally:
            os.environ.pop(H.ENV_AUTOSTART, None)
        self.assertFalse(H.autostart_enabled())

    def test_status(self):
        h = H.Harvester()
        st = h.status()
        self.assertIn("active", st)
        self.assertIn("cycles", st)
        self.assertIn("kg", st)

    def test_kg_from_dialog(self):
        h = H.Harvester(delay=(0, 0))
        h.start()
        bot = FakeBot()
        h.on_dialog(bot, 0, STYLE_LIST, "Сбор урожая", "Собрано 7 кг",
                    "Собрать", "", background=False)
        self.assertEqual(h.kg, 7.0)

    def test_kg_never_decreases(self):
        h = H.Harvester(delay=(0, 0))
        bot = FakeBot()
        h._bump_kg("Собрано 7 кг")
        h._bump_kg("Собрано 3 кг")
        self.assertEqual(h.kg, 7.0)


class EntryPointTests(unittest.TestCase):
    def test_handle_harvest_dialog_uses_bot_harvester(self):
        bot = FakeBot()
        bot.harvester = H.Harvester(delay=(0, 0))
        bot.harvester.start()
        handled = H.handle_harvest_dialog(
            bot, 0, STYLE_LIST, "Работы", "1. Сборщик", "", ""
        )
        self.assertTrue(handled)

    def test_handle_harvest_dialog_falls_back(self):
        bot = FakeBot()
        H.get_harvester().start()
        handled = H.handle_harvest_dialog(
            bot, 0, STYLE_LIST, "Работы", "1. Сборщик", "", ""
        )
        self.assertTrue(handled)


if __name__ == "__main__":
    unittest.main()
