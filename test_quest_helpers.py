"""Офлайн-тесты помощников сюжетки. Сервер не трогаем."""

import unittest

from logic import calls as calls_mod
from logic import npc_dialog as npc_mod
from logic import paint as paint_mod
from logic import shop as shop_mod
from logic.quest_helpers import QuestHelpers


class FakeApi:
    def __init__(self):
        self.npc_keys = []
        self.food = []
        self.call_answers = []

    def send_npc_dialog_response(self, key):
        self.npc_keys.append(key)
        return True

    def setbotfood(self, item_id):
        self.food.append(item_id)
        return True

    def send_call_notification_response(self, notification_type=2, button=1):
        self.call_answers.append((notification_type, button))
        return True


class FakeSession:
    def __init__(self):
        self.sent = []

    def send_json(self, interface_id, payload, packet_name="interface_sync"):
        self.sent.append((interface_id, payload))
        return True


class FakeBot:
    def __init__(self):
        self.api = FakeApi()
        self.session = FakeSession()


def npc_payload(name="Дядя Слава", text="Привет", buttons=None):
    if buttons is None:
        buttons = [{"bn": "Далее", "bk": 7, "bt": 0}]
    return {"o": 1, "n": name, "d": text, "b": buttons}


class NpcDialogTests(unittest.TestCase):
    def test_parse_buttons_keeps_keys(self):
        dialog = npc_mod.NpcDialog.from_payload(
            npc_payload(buttons=[{"bn": "Далее", "bk": 3}, {"bn": "Нет", "bk": 4}])
        )
        self.assertEqual([b["text"] for b in dialog.buttons], ["Далее", "Нет"])
        self.assertEqual([b["key"] for b in dialog.buttons], [3, 4])

    def test_best_button_prefers_dalshe(self):
        dialog = npc_mod.NpcDialog.from_payload(
            npc_payload(buttons=[{"bn": "Отказаться", "bk": 1}, {"bn": "Далее", "bk": 2}])
        )
        self.assertEqual(dialog.best_button()["key"], 2)

    def test_closing_button_rejected(self):
        dialog = npc_mod.NpcDialog.from_payload(
            npc_payload(buttons=[{"bn": "Отказаться", "bk": 1}])
        )
        self.assertIsNone(dialog.best_button())

    def test_missing_key_falls_back_to_index(self):
        dialog = npc_mod.NpcDialog.from_payload(
            npc_payload(buttons=[{"bn": "Далее"}, {"bn": "Ок"}])
        )
        self.assertEqual([b["key"] for b in dialog.buttons], [0, 1])

    def test_flow_advances_and_counts(self):
        bot = FakeBot()
        flow = npc_mod.NpcDialogFlow(bot, answer_delay=0.0)
        flow.on_payload(npc_payload())
        self.assertEqual(flow.advance(), 7)
        self.assertEqual(bot.api.npc_keys, [7])

    def test_flow_does_not_spam_same_key(self):
        bot = FakeBot()
        flow = npc_mod.NpcDialogFlow(bot, answer_delay=999.0)
        flow.on_payload(npc_payload())
        self.assertEqual(flow.advance(), 7)
        self.assertIsNone(flow.advance())
        self.assertEqual(len(bot.api.npc_keys), 1)

    def test_flow_stops_on_page_limit(self):
        bot = FakeBot()
        flow = npc_mod.NpcDialogFlow(bot, max_pages=2)
        flow.on_payload(npc_payload(text="1"))
        self.assertTrue(flow.on_payload(npc_payload(text="2")))
        self.assertFalse(flow.on_payload(npc_payload(text="3")))
        self.assertTrue(flow.closed)

    def test_choose_specific_button(self):
        bot = FakeBot()
        flow = npc_mod.NpcDialogFlow(bot, answer_delay=0.0)
        flow.on_payload(
            npc_payload(buttons=[{"bn": "Далее", "bk": 1}, {"bn": "Взять", "bk": 9}])
        )
        self.assertEqual(flow.choose("взять"), 9)
        self.assertEqual(bot.api.npc_keys, [9])

    def test_choose_missing_button_returns_none(self):
        bot = FakeBot()
        flow = npc_mod.NpcDialogFlow(bot, answer_delay=0.0)
        flow.on_payload(npc_payload())
        self.assertIsNone(flow.choose("купить"))
        self.assertEqual(bot.api.npc_keys, [])

    def test_close_resets_dialog(self):
        flow = npc_mod.NpcDialogFlow(FakeBot())
        flow.on_payload(npc_payload())
        flow.on_close()
        self.assertIsNone(flow.dialog)
        self.assertTrue(flow.closed)


class ShopTests(unittest.TestCase):
    def payload(self, items):
        return {"o": 1, "i": items}

    def test_parse_items(self):
        menu = shop_mod.ShopMenu.from_payload(
            self.payload([{"id": 5, "n": "Хлеб", "p": 40}, {"id": 6, "n": "Вода", "p": 25}])
        )
        self.assertEqual(len(menu.items), 2)
        self.assertEqual(menu.items[0].name, "Хлеб")

    def test_cheapest_wins(self):
        menu = shop_mod.ShopMenu.from_payload(
            self.payload([{"id": 5, "n": "Хлеб", "p": 40}, {"id": 6, "n": "Вода", "p": 25}])
        )
        self.assertEqual(menu.cheapest().item_id, 6)

    def test_by_name(self):
        menu = shop_mod.ShopMenu.from_payload(
            self.payload([{"id": 5, "n": "Хлеб", "p": 40}, {"id": 6, "n": "Вода", "p": 25}])
        )
        self.assertEqual(menu.by_name("вода").item_id, 6)

    def test_buy_cheapest_sends_shop_food(self):
        bot = FakeBot()
        runner = shop_mod.ShopRunner(bot)
        runner.on_payload(self.payload([{"id": 5, "n": "Хлеб", "p": 40}, {"id": 6, "p": 25}]))
        self.assertEqual(runner.buy(), 6)
        self.assertEqual(bot.api.food, [6])

    def test_buy_without_menu_fails_cleanly(self):
        runner = shop_mod.ShopRunner(FakeBot())
        self.assertIsNone(runner.buy())
        self.assertEqual(runner.last_error, "ларёк не открыт")

    def test_buy_named(self):
        bot = FakeBot()
        runner = shop_mod.ShopRunner(bot)
        runner.on_payload(self.payload([{"id": 5, "n": "Хлеб"}, {"id": 6, "n": "Вода"}]))
        self.assertEqual(runner.buy(name="хлеб"), 5)
        self.assertEqual(bot.api.food, [5])

    def test_empty_payload_ignored(self):
        self.assertIsNone(shop_mod.ShopMenu.from_payload({"o": 1}))


class CallTests(unittest.TestCase):
    def test_parse(self):
        call = calls_mod.IncomingCall.from_payload(
            {"o": 1, "t": 3, "h": "Дядя Слава", "s": "Подойди", "b": "Ответить"}
        )
        self.assertEqual(call.notification_type, 3)
        self.assertTrue(call.is_story())

    def test_answer_uses_server_type(self):
        bot = FakeBot()
        answerer = calls_mod.CallAnswerer(bot, story_only=True)
        self.assertTrue(answerer.on_payload({"o": 1, "t": 7, "h": "Дядя Слава", "s": "Алло"}))
        self.assertEqual(bot.api.call_answers, [(7, 1)])

    def test_non_story_skipped(self):
        bot = FakeBot()
        answerer = calls_mod.CallAnswerer(bot, story_only=True)
        self.assertFalse(answerer.on_payload({"o": 1, "t": 2, "h": "Реклама", "s": "Купи"}))
        self.assertEqual(bot.api.call_answers, [])
        self.assertEqual(answerer.skipped, 1)

    def test_missing_type_ignored(self):
        self.assertIsNone(calls_mod.IncomingCall.from_payload({"o": 1, "h": "Кто-то"}))

    def test_cooldown_blocks_second_answer(self):
        bot = FakeBot()
        answerer = calls_mod.CallAnswerer(bot, story_only=False, cooldown=999.0)
        self.assertTrue(answerer.on_payload({"o": 1, "t": 2, "h": "A", "s": "1"}))
        self.assertFalse(answerer.on_payload({"o": 1, "t": 2, "h": "B", "s": "2"}))
        self.assertEqual(len(bot.api.call_answers), 1)


class PaintTests(unittest.TestCase):
    def test_black_by_name(self):
        palette = paint_mod.ColorPalette.from_payload(
            {"c": [{"id": 1, "n": "Белый"}, {"id": 2, "n": "Чёрный"}]}
        )
        self.assertEqual(palette.black().option_id, 2)

    def test_black_by_hex(self):
        palette = paint_mod.ColorPalette.from_payload(
            {"c": [{"id": 1, "n": "x", "h": "FFFFFF"}, {"id": 2, "n": "y", "h": "000000"}]}
        )
        self.assertEqual(palette.black().option_id, 2)

    def test_black_yoba_variant(self):
        palette = paint_mod.ColorPalette.from_payload({"c": [{"id": 3, "n": "черный"}]})
        self.assertEqual(palette.black().option_id, 3)

    def test_no_black_returns_none(self):
        palette = paint_mod.ColorPalette.from_payload({"c": [{"id": 1, "n": "Красный"}]})
        self.assertIsNone(palette.black())

    def test_pick_sends_payload(self):
        bot = FakeBot()
        picker = paint_mod.PaintPicker(bot)
        picker.on_payload({"c": [{"id": 1, "n": "Белый"}, {"id": 2, "n": "Чёрный"}]})
        picker.interface_id = 77
        self.assertEqual(picker.pick_black(), 2)
        self.assertEqual(bot.session.sent, [(77, {"t": 1, "r": 2})])

    def test_pick_without_interface_id_fails(self):
        bot = FakeBot()
        picker = paint_mod.PaintPicker(bot)
        picker.on_payload({"c": [{"id": 2, "n": "Чёрный"}]})
        self.assertIsNone(picker.pick_black())
        self.assertEqual(bot.session.sent, [])

    def test_hex_normalization(self):
        self.assertEqual(paint_mod.normalize_hex("0x00FF00"), "#00FF00")
        self.assertEqual(paint_mod.normalize_hex("#fff000"), "#FFF000")
        self.assertIsNone(paint_mod.normalize_hex("мусор"))


class DispatcherTests(unittest.TestCase):
    def test_routes_npc(self):
        bot = FakeBot()
        h = QuestHelpers(bot, log=lambda m: None)
        self.assertEqual(h.on_json(63, npc_payload()), "npc")
        self.assertEqual(h.next(), 7)

    def test_routes_call(self):
        bot = FakeBot()
        h = QuestHelpers(bot, log=lambda m: None)
        self.assertEqual(h.on_json(65, {"o": 1, "t": 4, "h": "Дядя Слава", "s": "x"}), "call")
        self.assertEqual(bot.api.call_answers, [(4, 1)])

    def test_routes_shop(self):
        bot = FakeBot()
        h = QuestHelpers(bot, log=lambda m: None)
        self.assertEqual(h.on_json(3, {"o": 1, "i": [{"id": 6, "p": 25}]}), "shop")
        self.assertEqual(h.buy_food(), 6)

    def test_routes_paint_and_remembers_iid(self):
        bot = FakeBot()
        h = QuestHelpers(bot, log=lambda m: None)
        self.assertEqual(h.on_json(91, {"c": [{"id": 2, "n": "Чёрный"}]}), "paint")
        self.assertEqual(h.paint.interface_id, 91)
        self.assertEqual(h.pick_black(), 2)
        self.assertEqual(bot.session.sent, [(91, {"t": 1, "r": 2})])

    def test_unknown_interface_ignored(self):
        h = QuestHelpers(FakeBot(), log=lambda m: None)
        self.assertIsNone(h.on_json(999, {"o": 1}))

    def test_npc_close_marks_closed(self):
        h = QuestHelpers(FakeBot(), log=lambda m: None)
        h.on_json(63, npc_payload())
        self.assertEqual(h.on_json(63, {"c": 1}), "npc")
        self.assertTrue(h.npc.closed)

    def test_calls_can_be_disabled(self):
        bot = FakeBot()
        h = QuestHelpers(bot, log=lambda m: None, answer_calls=False)
        self.assertIsNone(h.on_json(65, {"o": 1, "t": 4, "h": "Дядя Слава", "s": "x"}))
        self.assertEqual(bot.api.call_answers, [])

    def test_status_shape(self):
        h = QuestHelpers(FakeBot(), log=lambda m: None)
        status = h.status()
        for key in ("npc", "shop", "calls", "paint", "last", "interfaces"):
            self.assertIn(key, status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
