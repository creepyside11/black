"""Юнит-тесты спавна: геометрия и RPC-хендшейк.

Запуск: python3 -m unittest test_spawn_unit -v
"""
import unittest

from samp.constants import PacketPriority, PacketReliability
from samp.packets import PacketBuilder

from logic import spawn as S

RC_PKT = PacketBuilder.rpc_build("request_class", 0)
SP_PKT = PacketBuilder.rpc_build("spawn")

SKY = (1469.6, 2495.1, 2501.0)
STATION = (2742.1, -2445.2, 21.8)
AUTH = (13.703383445739746, 485.29071044921875, -20.0)
ZERO = (0.0, 0.0, 0.0)


class FakeSync:
    def __init__(self):
        self.enabled = False
        self.started = 0

    def start(self):
        self.enabled = True
        self.started += 1


class FakeApi:
    def __init__(self, pos):
        self.pos = pos

    def getbotposition(self):
        return self.pos


class FakeProtocol:
    """Имитация сервера: отвечает ровно как живой Black Russia."""

    def __init__(self, api, *, ordered_answers=True):
        self.api = api
        self.ordered_answers = ordered_answers
        self.sent = []

    def send_with_header(self, payload, reliability, ordering_channel=0, priority=0):
        if payload == RC_PKT:
            name = "request_class"
        elif payload == SP_PKT:
            name = "spawn"
        else:
            name = "other"
        self.sent.append((name, reliability, priority))

        if name == "request_class" and reliability == PacketReliability.RELIABLE:
            self.api.pos = SKY
        elif name == "request_class" and reliability == PacketReliability.RELIABLE_ORDERED:
            if self.ordered_answers:
                self.api.pos = STATION

    def names(self):
        return [s[0] for s in self.sent]


class FakeSession:
    def __init__(self, api, protocol):
        self.connected = True
        self.protocol = protocol
        self.sync = FakeSync()

    def send_rpc_update_scores_and_pings(self):
        pass


class FakeBot:
    def __init__(self, pos=AUTH, *, ordered_answers=True):
        self.api = FakeApi(pos)
        self.protocol = FakeProtocol(self.api, ordered_answers=ordered_answers)
        self.session = FakeSession(self.api, self.protocol)
        self.marked = []
        self.logs = []

    def mark_spawned(self, position=None):
        self.marked.append(position or self.api.pos)
        return True

    def log(self, msg):
        self.logs.append(msg)


class GeometryTests(unittest.TestCase):
    def test_sky(self):
        self.assertTrue(S.in_sky(SKY))
        self.assertFalse(S.in_sky(STATION))
        self.assertFalse(S.in_sky(None))

    def test_auth_zone(self):
        self.assertTrue(S.in_auth_zone(AUTH))
        self.assertTrue(S.in_auth_zone(ZERO))
        self.assertFalse(S.in_auth_zone(STATION))

    def test_spawned_position(self):
        self.assertTrue(S.is_spawned_position(STATION))
        self.assertFalse(S.is_spawned_position(SKY))
        self.assertFalse(S.is_spawned_position(AUTH))
        self.assertFalse(S.is_spawned_position(ZERO))
        self.assertFalse(S.is_spawned_position(None))

    def test_nearest_spawn(self):
        name, dist = S.nearest_spawn_name(STATION)
        self.assertEqual(name, "Южный")
        self.assertLess(dist, 15.0)

        name, _ = S.nearest_spawn_name((1795.0, 2518.0, 14.5))
        self.assertEqual(name, "Батырево")

    def test_unknown_station_is_not_guessed(self):
        # Вокзал не из таблицы: ближайшая точка в 2км — это не «почти он».
        far = (831.0, 778.4, 13.3)
        name, dist = S.nearest_spawn_name(far)
        self.assertIsNone(name)
        self.assertGreater(dist, S.STATION_RADIUS)
        self.assertIn("831", S.station_name(far))

    def test_remember_station_learns_new_point(self):
        before = set(S.SPAWNS)
        point = (831.0, 778.4, 13.3)
        S.remember_station(point)
        try:
            added = set(S.SPAWNS) - before
            self.assertEqual(len(added), 1)
            self.assertEqual(S.nearest_spawn_name(point)[0], added.pop())
        finally:
            for key in set(S.SPAWNS) - before:
                S.SPAWNS.pop(key, None)

    def test_remember_station_skips_non_spawn(self):
        before = set(S.SPAWNS)
        S.remember_station(AUTH)
        S.remember_station(SKY)
        self.assertEqual(set(S.SPAWNS), before)


class HandshakeTests(unittest.TestCase):
    def test_delivery_params(self):
        bot = FakeBot()
        S.request_class(bot, 0, ordered=False)
        S.request_class(bot, 0, ordered=True)
        S.send_spawn(bot, ordered=False)
        S.send_spawn(bot, ordered=True)

        self.assertEqual(
            bot.protocol.sent,
            [
                ("request_class", PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY),
                ("request_class", PacketReliability.RELIABLE_ORDERED, PacketPriority.MEDIUM_PRIORITY),
                ("spawn", PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY),
                ("spawn", PacketReliability.RELIABLE_ORDERED, PacketPriority.MEDIUM_PRIORITY),
            ],
        )

    def test_force_spawn_happy_path(self):
        bot = FakeBot()
        report = S.force_spawn(
            bot, log=bot.log, step_delay=0.0, timeout=1.0, stable_seconds=0.0
        )
        self.assertEqual(report["status"], "spawned")
        self.assertEqual(report["station"], "Южный")
        self.assertEqual(bot.protocol.names()[:3], ["request_class", "spawn", "request_class"])
        self.assertTrue(bot.session.sync.enabled)

    def test_force_spawn_already_spawned(self):
        bot = FakeBot(pos=STATION)
        report = S.force_spawn(bot, step_delay=0.0, timeout=0.5, stable_seconds=0.0)
        self.assertEqual(report["status"], "already_spawned")
        self.assertEqual(bot.protocol.sent, [])

    def test_force_spawn_no_session(self):
        bot = FakeBot()
        bot.session.connected = False
        report = S.force_spawn(bot, step_delay=0.0, timeout=0.5)
        self.assertEqual(report["status"], "no_session")

    def test_force_spawn_failure(self):
        bot = FakeBot(ordered_answers=False)
        report = S.force_spawn(
            bot, step_delay=0.0, timeout=0.3, stable_seconds=0.0, attempts=2
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["attempts"], 2)
        # попытка 1: rc(372) + rc(ordered); попытка 2 уже из неба — только ordered
        self.assertEqual(bot.protocol.names().count("request_class"), 3)

    def test_force_spawn_from_sky_skips_first_step(self):
        bot = FakeBot(pos=SKY)
        report = S.force_spawn(bot, step_delay=0.0, timeout=1.0, stable_seconds=0.0)
        self.assertEqual(report["status"], "spawned")
        # 372 у сервера уже отдан — первый request_class не шлём
        self.assertEqual(bot.protocol.names()[0], "spawn")


class WaitSpawnedTests(unittest.TestCase):
    def test_returns_false_for_auth_zone(self):
        bot = FakeBot(pos=AUTH)
        ok, pos = S.wait_spawned(bot, 0.3, stable_seconds=0.0, poll=0.05)
        self.assertFalse(ok)

    def test_requires_stable_window(self):
        bot = FakeBot(pos=SKY)

        def flip():
            import time as _t
            _t.sleep(0.1)
            bot.api.pos = STATION
            _t.sleep(0.1)
            bot.api.pos = SKY

        import threading
        threading.Thread(target=flip, daemon=True).start()
        ok, _ = S.wait_spawned(bot, 0.6, stable_seconds=0.4, poll=0.05)
        self.assertFalse(ok)

    def test_accepts_stable_spawn(self):
        bot = FakeBot(pos=STATION)
        ok, pos = S.wait_spawned(bot, 0.5, stable_seconds=0.1, poll=0.02)
        self.assertTrue(ok)
        self.assertEqual(pos, STATION)


class WatchdogTests(unittest.TestCase):
    def test_watchdog_spawns_and_marks(self):
        bot = FakeBot()
        wd = S.SpawnWatchdog(bot, log=bot.log, attempt_timeout=1.0, stable_seconds=0.0)
        wd.attempt_timeout = 1.0
        S.force_spawn(bot, step_delay=0.0, timeout=1.0, stable_seconds=0.0)
        # после хендшейка сторож просто фиксирует факт
        self.assertTrue(S.is_spawned_position(bot.api.pos))
        self.assertFalse(wd.running)

    def test_watchdog_marks_already_spawned(self):
        bot = FakeBot(pos=STATION)
        wd = S.SpawnWatchdog(bot, log=bot.log, attempt_timeout=0.5, stable_seconds=0.0)
        wd._mark_spawned(bot.api.pos)
        self.assertEqual(bot.marked, [STATION])

    def test_watchdog_dies_offline(self):
        bot = FakeBot()
        bot.session.connected = False
        wd = S.SpawnWatchdog(bot, log=bot.log)
        wd.OFFLINE_GRACE = 0.2
        wd.start()
        import time
        time.sleep(1.5)
        self.assertFalse(wd.running)


class MenuTests(unittest.TestCase):
    MENU = {"o": 1, "m": [1, 2]}

    def tearDown(self):
        S.reset_menu()

    def test_menu_detected(self):
        self.assertTrue(S.is_spawn_menu(self.MENU))
        self.assertFalse(S.is_spawn_menu({"s": 1}))
        self.assertFalse(S.is_spawn_menu({"o": 1, "m": []}))
        self.assertFalse(S.is_spawn_menu({}))

    def test_remember_menu(self):
        items = S.remember_spawn_menu(self.MENU)
        self.assertEqual(items, [1, 2])
        self.assertEqual(S.available_spawns(), [1, 2])
        self.assertTrue(S.menu_received_recent())

    def test_choice_is_first_id_not_index(self):
        # Индексы в меню — это id вокзалов. Берём первый, а не 1.
        self.assertEqual(S.menu_choice([7, 9]), 7)
        self.assertEqual(S.build_menu_answer([7, 9]), {"t": 1, "s": 7})
        self.assertEqual(S.build_menu_answer([]), {"t": 1, "s": 1})

    def test_reset_menu(self):
        S.remember_spawn_menu(self.MENU)
        S.reset_menu()
        self.assertEqual(S.available_spawns(), [])
        self.assertFalse(S.menu_received_recent())

    def test_select_spawn_sends_first_choice(self):
        bot = FakeBot(pos=AUTH)
        S.remember_spawn_menu(self.MENU)
        sent = []
        bot.session.send_json = lambda iid, payload, packet_name=None: sent.append((iid, payload)) or True
        self.assertTrue(S.select_spawn(bot))
        self.assertEqual(sent, [(50, {"t": 1, "s": 1})])
        self.assertTrue(S.menu_answer_recent())


class TravelTests(unittest.TestCase):
    def test_nearest_other_station(self):
        name, coords, dist = S.nearest_other_station((1795.0, 2518.0, 14.5))
        self.assertNotEqual(name, "Батырево")
        self.assertIsNotNone(coords)
        self.assertGreater(dist, 0)

    def test_travel_unknown_station(self):
        bot = FakeBot(pos=STATION)
        result = S.travel_to_station(bot, "нет такого")
        self.assertEqual(result["status"], "unknown_station")

    def test_travel_no_session(self):
        bot = FakeBot(pos=STATION)
        bot.session.connected = False
        result = S.travel_to_station(bot, "Батырево")
        self.assertEqual(result["status"], "no_session")


class EnsureSpawnTests(unittest.TestCase):
    def test_ensure_spawn_ok(self):
        bot = FakeBot()
        seen = []
        report = S.ensure_spawn(
            bot, timeout=3.0, log=bot.log, on_success=seen.append
        )
        self.assertIn(report["status"], ("spawned", "already_spawned"))
        self.assertEqual(len(seen), 1)

    def test_ensure_spawn_no_session(self):
        bot = FakeBot()
        bot.session.connected = False
        report = S.ensure_spawn(bot, timeout=1.0)
        self.assertEqual(report["status"], "no_session")


if __name__ == "__main__":
    unittest.main()


class SpawnAnswerDelayTests(unittest.TestCase):
    """Ответ на меню вокзала не должен уходить мгновенно.

    Живой случай: меню пришло на 15.3с, ответ ушёл на 15.3с, кик античита
    кодом 25 на 15.6с. Тот вход, где меню не пришло вовсе, прошёл спокойно.
    """

    def test_delay_is_human_like(self):
        from logic import spawn as sp
        for _ in range(200):
            pause = sp.spawn_answer_delay()
            self.assertGreaterEqual(pause, sp.SPAWN_ANSWER_DELAY[0])
            self.assertLessEqual(pause, sp.SPAWN_ANSWER_DELAY[1])

    def test_delay_is_not_constant(self):
        """Постоянная пауза — тоже признак: живой человек не робот."""
        from logic import spawn as sp
        values = {round(sp.spawn_answer_delay(), 3) for _ in range(50)}
        self.assertGreater(len(values), 10)

    def test_delay_bounds_sane(self):
        """Меньше секунды смысла нет, больше пяти — меню истечёт."""
        from logic import spawn as sp
        low, high = sp.SPAWN_ANSWER_DELAY
        self.assertGreaterEqual(low, 1.0)
        self.assertLessEqual(high, 5.0)
