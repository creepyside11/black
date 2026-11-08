"""Тесты движка Славы по эталонной записи _diag/slava_new.pcap.

Проверяем ровно то, что делает чужой клиент в захвате 06.11:
маркер -> RPC 423, ответы на iid 63/65/114/13/10/3/2/87/127/128/73,
команды /e и /healme, автоподбор пикапа на маркере.
"""
import struct
import time
import unittest

import logic.slava as S


class FakePlayer:
    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.pos = list(pos)
        self.in_veh = False
        self.vid = 0

    def get_position(self):
        return tuple(self.pos)

    def update_position(self, x, y, z, *a, **kw):
        self.pos = [float(x), float(y), float(z)]


class FakeStream:
    def __init__(self):
        self.pickups = {}


class FakeSession:
    connected = True

    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.players = type("P", (), {"bot": FakePlayer(pos)})()
        self.stream = FakeStream()


class FakeApi:
    def __init__(self):
        self.rpcs = []
        self.json = []
        self.chat = []
        self.pickups = []

    def SendRPC(self, rpc_id, payload=b"", **kw):
        self.rpcs.append((int(rpc_id), bytes(payload)))

    def send_json(self, iid, data, **kw):
        self.json.append((int(iid), dict(data)))
        return True

    def sendchat(self, text):
        self.chat.append(text)
        return True

    def SendPickup(self, pickup_id):
        self.pickups.append(int(pickup_id))


class FakeBot:
    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.session = FakeSession(pos)
        self.api = FakeApi()
        self.logs = []

    def add_log(self, text):
        self.logs.append(str(text))


def make(pos=(0.0, 0.0, 0.0)):
    bot = FakeBot(pos)
    q = S.SlavaQuest(bot, log=lambda m: bot.logs.append(m))
    q.running = True
    return bot, q


def wait(flag, limit=200):
    for _ in range(limit):
        if flag():
            return True
        time.sleep(0.01)
    return False


class RouteTests(unittest.TestCase):
    def test_exact_route(self):
        point = S.route_target("Перекусить в уличном киоске")
        self.assertEqual(point, (2744.19, -2429.08, 21.82))

    def test_route_with_formatting(self):
        point = S.route_target("{FFFFFF}Купить аптечку")
        self.assertEqual(point, (1547.16, 414.35, 1001.04))

    def test_unknown_route(self):
        self.assertIsNone(S.route_target("Погладить кота"))


class MapIconTests(unittest.TestCase):
    def test_marker_sets_target(self):
        bot, q = make()
        self.assertTrue(q.on_map_icon(0, (2749.43, -2441.77, 21.81), 1, 1))
        self.assertEqual(q.target, (2749.43, -2441.77, 21.81))

    def test_marker_bad_values(self):
        bot, q = make()
        self.assertFalse(q.on_map_icon(0, ("x", 1, 2)))

    def test_marker_ignored_when_stopped(self):
        bot, q = make()
        q.running = False
        self.assertFalse(q.on_map_icon(0, (1.0, 2.0, 3.0)))


class TeleportTests(unittest.TestCase):
    """Движение — delta-телепортом, а не RPC 423.

    RPC 423 в записи 06.11 идёт ОТ сервера (set_player_pos), клиент его не
    шлёт. Отправка наверх ничего не делает: сервер держит бота на месте.
    """

    def test_jump_uses_delta(self):
        bot, q = make()
        calls = []
        bot.api.DeltaTeleportTo = lambda x, y, z, **kw: calls.append((x, y, z))
        ok, how = q.teleport.jump((10.0, 20.0, 30.0))
        self.assertTrue(ok)
        self.assertIn("delta", how)
        self.assertEqual(calls, [(10.0, 20.0, 30.0)])
        self.assertEqual(bot.api.rpcs, [])

    def test_go_reports_distance_and_speed(self):
        bot, q = make()
        bot.api.DeltaTeleportTo = lambda x, y, z, **kw: bot.session.players.bot.update_position(x, y, z)
        note = q.teleport.go((30.0, 40.0, 0.0))
        self.assertIn("перемещение", note)
        self.assertIn("50.0 м", note)

    def test_go_skips_when_close(self):
        bot, q = make(pos=(1.0, 1.0, 1.0))
        note = q.teleport.go((1.0, 1.0, 1.0))
        self.assertIn("уже на месте", note)

    def test_fallback_to_setbotposition(self):
        bot, q = make()
        calls = []

        def boom(*a, **kw):
            raise RuntimeError("нет delta")

        bot.api.DeltaTeleportTo = boom
        bot.api.setbotposition = lambda x, y, z: calls.append((x, y, z))
        ok, how = q.teleport.jump((1.0, 2.0, 3.0))
        self.assertTrue(ok)
        self.assertEqual(how, "setbotposition")
        self.assertEqual(calls, [(1.0, 2.0, 3.0)])


class DialogTests(unittest.TestCase):
    def test_speech_closes_after_duration(self):
        bot, q = make()
        original = S.SPEECH_TAIL
        S.SPEECH_TAIL = (0.0, 0.0)
        try:
            self.assertTrue(q.on_dialog_npc(0.05, "Дядя Слава: привет"))
            self.assertTrue(wait(lambda: bot.api.json))
        finally:
            S.SPEECH_TAIL = original
        self.assertEqual(bot.api.json[0], (S.NPC_SPEECH_IID, {"c": 1}))

    def test_notice_dismiss_payload(self):
        bot, q = make()
        original = S.NOTICE_DISMISS
        S.NOTICE_DISMISS = (0.0, 0.0)
        try:
            data = {"o": 1, "t": 3, "i": "Уровень выше", "s": -1, "b": 1}
            self.assertTrue(q.on_notification(data))
            self.assertTrue(wait(lambda: bot.api.json))
        finally:
            S.NOTICE_DISMISS = original
        self.assertEqual(
            bot.api.json[0][1], {"c": 13, "t": 1, "s": -1, "b": 1}
        )

    def test_skip_trip_uses_server_button(self):
        bot, q = make()
        original = S.NOTICE_CONFIRM
        S.NOTICE_CONFIRM = (0.0, 0.0)
        try:
            data = {"o": 1, "t": 5, "i": "Хочешь пропустить поездку?",
                    "d": 10, "s": 71, "k": "Да", "b": 0}
            self.assertTrue(q.on_notification(data))
            self.assertTrue(wait(lambda: bot.api.json))
        finally:
            S.NOTICE_CONFIRM = original
        self.assertEqual(
            bot.api.json[0][1], {"c": 13, "t": 0, "s": 71, "b": 0}
        )


class ShopTests(unittest.TestCase):
    def _fast(self, name, value):
        original = getattr(S, name)
        setattr(S, name, value)
        self.addCleanup(setattr, S, name, original)

    def test_food_payload(self):
        bot, q = make()
        self._fast("SHOP_DELAY", (0.0, 0.0))
        self.assertTrue(q.on_shop_food({"o": 1, "m": 100000}))
        self.assertTrue(wait(lambda: bot.api.json))
        self.assertEqual(bot.api.json[0], (S.SHOP_FOOD_IID, {"t": 1, "r": 32}))

    def test_fuel_payload(self):
        bot, q = make()
        self._fast("FUEL_DELAY", (0.0, 0.0))
        self.assertTrue(q.on_fuel({"o": 1, "m": 112}))
        self.assertTrue(wait(lambda: bot.api.json))
        self.assertEqual(
            bot.api.json[0], (S.FUEL_IID, {"f": 1, "t": 0, "v": 1})
        )

    def test_prompt_payload(self):
        bot, q = make()
        self._fast("PROMPT_DELAY", (0.0, 0.0))
        self.assertTrue(q.on_prompt({"o": 1, "id": 22}))
        self.assertTrue(wait(lambda: bot.api.json))
        self.assertEqual(bot.api.json[0], (S.PROMPT_IID, {"c": 1}))

    def test_menu_picks_medkit(self):
        bot, q = make()
        self._fast("MENU_PICK_DELAY", (0.0, 0.0))
        q.on_quest_text("Купить аптечку")
        items = [{"n": "1. Еда"}, {"n": "2. Аптечка"}]
        self.assertTrue(q.on_menu({"o": 1, "m": items}))
        self.assertTrue(wait(lambda: bot.api.json))
        iid, payload = bot.api.json[0]
        self.assertEqual(iid, S.MENU_IID)
        self.assertEqual(payload["l"], 1)
        self.assertIn("Аптечка", payload["i"])

    def test_menu_closes_when_nothing_needed(self):
        bot, q = make()
        self._fast("MENU_PICK_DELAY", (0.0, 0.0))
        q.on_quest_text("Доезжайте до шахты")
        self.assertTrue(q.on_menu({"o": 1, "m": [{"n": "1. Еда"}]}))
        self.assertTrue(wait(lambda: bot.api.json))
        self.assertEqual(bot.api.json[0][1], S.MENU_CLOSE)


class GiftTests(unittest.TestCase):
    def test_donate_opens_gift(self):
        bot, q = make()
        self.assertTrue(q.on_donate({"o": 1, "pg": 1}))
        deadline = time.time() + 4.0
        while time.time() < deadline and not bot.api.json:
            time.sleep(0.02)
        self.assertEqual(
            bot.api.json[0], (S.DONATE_IID, {"cmd": 4, "id": 4, "pg": 1})
        )

    def test_case_reward_collected(self):
        bot, q = make()
        original = S.PROMPT_DELAY
        S.PROMPT_DELAY = (0.0, 0.0)
        try:
            self.assertTrue(q.on_case_reward({"o": 1, "tit": "Билет"}))
            self.assertTrue(wait(lambda: bot.api.json))
        finally:
            S.PROMPT_DELAY = original
        self.assertEqual(bot.api.json[0], (S.CASE_REWARD_IID, {"c": 1}))

    def test_quest_text_requests_donate(self):
        bot, q = make()
        q.on_quest_text("Нажми [Получить], чтобы открыть свой подарок")
        self.assertIn("/donate", bot.api.chat)


class PickupTests(unittest.TestCase):
    def test_canister_picked_at_marker(self):
        bot, q = make(pos=(2241.5, -1783.6, 21.8))
        pickup = type("PU", (), {
            "pickup_id": 2048, "model_id": 1650,
            "x": 2241.57, "y": -1783.60, "z": 21.76,
        })()
        bot.session.stream.pickups = {2048: pickup}
        q._pickup_at = 0.0
        q._pickup_tick()
        self.assertEqual(bot.api.pickups, [2048])
        self.assertTrue(any("пикап 2048" in line for line in bot.logs))

    def test_far_pickup_ignored(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        pickup = type("PU", (), {
            "pickup_id": 7, "model_id": 1650,
            "x": 500.0, "y": 0.0, "z": 0.0,
        })()
        bot.session.stream.pickups = {7: pickup}
        q._pickup_at = 0.0
        q._pickup_tick()
        self.assertEqual(bot.api.pickups, [])

    def test_pickup_not_repeated(self):
        bot, q = make(pos=(2241.5, -1783.6, 21.8))
        pickup = type("PU", (), {
            "pickup_id": 2048, "model_id": 1650,
            "x": 2241.57, "y": -1783.60, "z": 21.76,
        })()
        bot.session.stream.pickups = {2048: pickup}
        q._pickup_at = 0.0
        q._pickup_tick()
        q._pickup_at = 0.0
        q._pickup_tick()
        self.assertEqual(bot.api.pickups, [2048])


class ConstantTests(unittest.TestCase):
    def test_interfaces_from_capture(self):
        self.assertEqual(S.NPC_SPEECH_IID, 114)
        self.assertEqual(S.MENU_IID, 10)
        self.assertEqual(S.FUEL_IID, 2)
        self.assertEqual(S.PROMPT_IID, 87)
        self.assertEqual(S.DONATE_IID, 127)
        self.assertEqual(S.CASE_REWARD_IID, 128)
        self.assertEqual(S.CASES_IID, 73)

    def test_no_client_teleport_rpc(self):
        # 423 — серверный rpc, в константах клиента его быть не должно.
        self.assertFalse(hasattr(S, "TELEPORT_RPC"))

    def test_food_item_id(self):
        self.assertEqual(S.SHOP_FOOD_ITEM, 32)


if __name__ == "__main__":
    unittest.main()
