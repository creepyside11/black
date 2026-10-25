"""Юнит-тесты спавна: геометрия и RPC-хендшейк.

Запуск: python3 -m unittest test_spawn_unit -v
"""
import time
import unittest
from unittest import mock

from samp.constants import PacketPriority, PacketReliability
from samp.packets import PacketBuilder

from logic import spawn as S
from logic import spawn as SP

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
        # JSON-пакеты (меню вокзалов) — отдельно от RPC.
        self.json_sent = []
        # Что сервер делает в ответ на выбор вокзала.
        self.on_spawn_select = None

    def send_rpc_update_scores_and_pings(self):
        pass

    def send_json(self, iid, payload, packet_name=None):
        self.json_sent.append((iid, payload))
        if self.on_spawn_select is not None:
            self.on_spawn_select(iid, payload)
        return True


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
        # Точка далеко от всех известных вокзалов: ближайший в километрах —
        # это не «почти он». Координата (831, 778) раньше была свободной,
        # но теперь это Арзамас, поэтому берём заведомо пустое место.
        far = (-1500.0, -1500.0, 5.0)
        name, dist = S.nearest_spawn_name(far)
        self.assertIsNone(name)
        self.assertGreater(dist, S.STATION_RADIUS)
        self.assertIn("-1500", S.station_name(far))

    def test_remember_station_learns_new_point(self):
        before = set(S.SPAWNS)
        point = (-1500.0, -1500.0, 5.0)
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
    """Механика самого хендшейка: reliability, приоритеты, шаги.

    В проде хендшейк выключен (ALLOW_RPC_SPAWN_HANDSHAKE = False) — он и
    давал кик кодом 25. Здесь включаем его на время теста, иначе проверять
    нечего: функции остались, просто их никто не зовёт.
    """

    def setUp(self):
        self._orig = S.ALLOW_RPC_SPAWN_HANDSHAKE
        S.ALLOW_RPC_SPAWN_HANDSHAKE = True

    def tearDown(self):
        S.ALLOW_RPC_SPAWN_HANDSHAKE = self._orig

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
    def test_force_spawn_puts_bot_on_station(self):
        """Сам force_spawn рабочий — просто в проде его больше никто не зовёт.

        Тест держит механику живой: если однажды понадобится аварийный
        путь, он не окажется сломанным. Хендшейк включаем только здесь.
        """
        bot = FakeBot()
        wd = S.SpawnWatchdog(bot, log=bot.log, attempt_timeout=1.0, stable_seconds=0.0)
        wd.attempt_timeout = 1.0
        orig = S.ALLOW_RPC_SPAWN_HANDSHAKE
        S.ALLOW_RPC_SPAWN_HANDSHAKE = True
        try:
            report = S.force_spawn(bot, step_delay=0.0, timeout=1.0, stable_seconds=0.0)
        finally:
            S.ALLOW_RPC_SPAWN_HANDSHAKE = orig
        self.assertIn(report.get("status"), ("spawned", "already_spawned"))
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

    def setUp(self):
        self.bot = FakeBot(pos=AUTH)

    def tearDown(self):
        S.reset_menu(self.bot)

    def test_menu_detected(self):
        self.assertTrue(S.is_spawn_menu(self.MENU))
        self.assertFalse(S.is_spawn_menu({"s": 1}))
        self.assertFalse(S.is_spawn_menu({"o": 1, "m": []}))
        self.assertFalse(S.is_spawn_menu({}))

    def test_remember_menu(self):
        items = S.remember_spawn_menu(self.MENU, self.bot)
        self.assertEqual(items, [1, 2])
        self.assertEqual(S.available_spawns(self.bot), [1, 2])
        self.assertTrue(S.menu_received_recent(self.bot))

    def test_choice_is_first_id_not_index(self):
        # Индексы в меню — это id вокзалов. Берём первый, а не 1.
        self.assertEqual(S.menu_choice([7, 9]), 7)
        self.assertEqual(S.build_menu_answer([7, 9]), {"t": 1, "s": 7})
        self.assertEqual(S.build_menu_answer([]), {"t": 1, "s": 1})

    def test_reset_menu(self):
        S.remember_spawn_menu(self.MENU, self.bot)
        S.reset_menu(self.bot)
        self.assertEqual(S.available_spawns(self.bot), [])
        self.assertFalse(S.menu_received_recent(self.bot))

    def test_select_spawn_sends_first_choice(self):
        bot = FakeBot(pos=AUTH)
        S.remember_spawn_menu(self.MENU, bot)
        sent = []
        bot.session.send_json = lambda iid, payload, packet_name=None: sent.append((iid, payload)) or True
        self.assertTrue(S.select_spawn(bot))
        self.assertEqual(sent, [(50, {"t": 1, "s": 1})])
        self.assertTrue(S.menu_answer_recent(bot))

    def test_two_bots_do_not_share_menu(self):
        """Меню одного бота не должно протекать к другому.

        Именно на этом ломался вход при многих ботах: бот B видел «меню
        только что приходило» от бота A и отвечал на чужое меню.
        """
        a = FakeBot(pos=AUTH)
        b = FakeBot(pos=AUTH)
        S.remember_spawn_menu({"o": 1, "m": [3, 4]}, a)

        self.assertEqual(S.available_spawns(a), [3, 4])
        self.assertEqual(S.available_spawns(b), [], "меню протекло к другому боту")
        self.assertTrue(S.menu_received_recent(a))
        self.assertFalse(S.menu_received_recent(b))

        # Вход A не должен стирать меню B.
        S.remember_spawn_menu({"o": 1, "m": [7, 8]}, b)
        S.reset_menu(a)
        self.assertEqual(S.available_spawns(b), [7, 8], "reset_menu(a) стёр меню b")

    def test_concurrent_answer_sends_once(self):
        """Сторож и обработчик меню не должны ответить дважды.

        Пауза перед отправкой идёт до пакета, а дедуп раньше стоял до
        паузы: оба потока проходили проверку и оба отправляли SPAWN_SELECT.
        Два одинаковых ответа на одно меню античит кикает кодом 25.
        """
        import threading

        bot = FakeBot(pos=AUTH)
        S.remember_spawn_menu(self.MENU, bot)
        sent = []
        lock = threading.Lock()

        def send_json(iid, payload, packet_name=None):
            with lock:
                sent.append((iid, payload))
            return True

        bot.session.send_json = send_json
        threads = [
            threading.Thread(target=lambda: S.select_spawn(bot, delay=0.2))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(sent), 1, f"отправлено {len(sent)} ответов вместо одного")
        self.assertEqual(sent[0], (50, {"t": 1, "s": 1}))

    def test_answer_retried_after_send_failure(self):
        """Сорвавшаяся отправка не должна блокировать следующий ответ."""
        bot = FakeBot(pos=AUTH)
        S.remember_spawn_menu(self.MENU, bot)
        bot.session.send_json = lambda iid, payload, packet_name=None: False
        self.assertFalse(S.select_spawn(bot, delay=0))
        self.assertFalse(S.menu_answer_recent(bot))

        sent = []
        bot.session.send_json = lambda iid, payload, packet_name=None: sent.append(payload) or True
        self.assertTrue(S.select_spawn(bot, delay=0))
        self.assertEqual(sent, [{"t": 1, "s": 1}])

    def test_two_bots_do_not_share_answer(self):
        """Ответ одного бота не должен считаться ответом другого."""
        a = FakeBot(pos=AUTH)
        b = FakeBot(pos=AUTH)
        for bot in (a, b):
            S.remember_spawn_menu(self.MENU, bot)
            bot.session.send_json = lambda iid, payload, packet_name=None: True

        self.assertTrue(S.select_spawn(a, delay=0))
        self.assertTrue(S.menu_answer_recent(a))
        self.assertFalse(S.menu_answer_recent(b), "ответ протёк к другому боту")


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
    """ensure_spawn ходит только через меню: RPC-хендшейк — кик кодом 25."""

    def setUp(self):
        # Человеческая пауза перед ответом на меню проверяется отдельно;
        # здесь она только замедляет тест.
        patcher = mock.patch.object(S, "spawn_answer_delay", lambda rng=None: 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bot_with_menu(self, items=(1, 2)):
        bot = FakeBot()
        S.remember_spawn_menu({"o": 1, "m": list(items)}, bot)
        bot.session.on_spawn_select = lambda iid, payload: setattr(bot.api, "pos", STATION)
        self.addCleanup(S.reset_menu, bot)
        return bot

    def test_ensure_spawn_ok(self):
        bot = self._bot_with_menu()
        seen = []
        report = S.ensure_spawn(
            bot, timeout=3.0, log=bot.log, on_success=seen.append, stable_seconds=0.0
        )
        self.assertEqual(report["status"], "spawned")
        self.assertEqual(report["position"], STATION)
        self.assertEqual(report["station"], "Южный")
        self.assertEqual(len(seen), 1)
        # Ответ на меню — ровно один SPAWN_SELECT.
        self.assertEqual(bot.session.json_sent, [(50, {"t": 1, "s": 1})])
        # И ни одного RPC: force_spawn в этом пути не участвует.
        self.assertEqual(bot.protocol.sent, [], "ensure_spawn отправил RPC-хендшейк")
        self.assertTrue(bot.session.sync.enabled)

    def test_ensure_spawn_never_calls_force_spawn(self):
        bot = self._bot_with_menu()
        with mock.patch.object(S, "force_spawn") as force_mock:
            report = S.ensure_spawn(
                bot, timeout=3.0, log=bot.log, stable_seconds=0.0
            )
        self.assertEqual(report["status"], "spawned")
        self.assertEqual(force_mock.call_count, 0, "ensure_spawn позвал force_spawn")

    def test_ensure_spawn_no_menu(self):
        """Меню не будет — сразу no_menu, без хендшейка и без ожидания таймаута."""
        bot = FakeBot()
        seen = []
        started = time.monotonic()
        with mock.patch.object(S, "force_spawn") as force_mock, \
                mock.patch.object(S, "wait_spawn_menu", lambda b, t, **kw: []):
            report = S.ensure_spawn(
                bot, timeout=3.0, log=bot.log, on_success=seen.append
            )
        self.assertEqual(report["status"], "no_menu")
        self.assertEqual(seen, [], "on_success позвали без спавна")
        self.assertEqual(force_mock.call_count, 0)
        self.assertEqual(bot.protocol.sent, [])
        self.assertEqual(bot.session.json_sent, [])
        self.assertLess(time.monotonic() - started, 3.0, "ждали полный таймаут впустую")

    def test_ensure_spawn_no_session(self):
        bot = FakeBot()
        bot.session.connected = False
        report = S.ensure_spawn(bot, timeout=1.0)
        self.assertEqual(report["status"], "no_session")


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


if __name__ == "__main__":
    unittest.main()


class NoHandshakeTests(unittest.TestCase):
    """RPC-хендшейк спавна выключен наглухо.

    Именно он давал кик античитом кодом 25 «постороннее ПО»: сервер
    видит подтверждение класса и спавн от клиента, который ещё не прошёл
    меню вокзалов. Спавн должен идти одним путём — ответом на меню.
    """

    def test_flag_is_off(self):
        self.assertFalse(SP.ALLOW_RPC_SPAWN_HANDSHAKE)

    def test_request_class_sends_nothing(self):
        bot = mock.Mock()
        sent = []
        bot.session = mock.Mock()
        bot.session._sender = mock.Mock()
        bot.session._sender.send = lambda *a, **k: sent.append(a)
        self.assertFalse(SP.request_class(bot, 0))
        self.assertEqual(sent, [], "request_class всё-таки отправился")

    def test_send_spawn_sends_nothing(self):
        bot = mock.Mock()
        sent = []
        bot.session = mock.Mock()
        bot.session._sender = mock.Mock()
        bot.session._sender.send = lambda *a, **k: sent.append(a)
        self.assertFalse(SP.send_spawn(bot))
        self.assertEqual(sent, [], "RPC spawn всё-таки отправился")

    def test_flag_does_not_touch_session(self):
        """Флаг сессии не должен включаться: иначе оживёт автоответ на 344."""
        bot = mock.Mock()
        bot.session = mock.Mock()
        bot.session.spawn_handshake_requested = False
        SP.request_class(bot, 0)
        self.assertFalse(bot.session.spawn_handshake_requested)


class NoPermanentBanOnKick25Tests(unittest.TestCase):
    """Кик кодом 25 не должен перманентно банить аккаунт.

    Причина кика была наша: RPC-хендшейк спавна при открытом меню. Так
    «отлетало» большинство аккаунтов, хотя на сервере они целы. Теперь
    это пауза, а не бан.
    """

    def test_source_does_not_ban_on_anticheat_kick(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        # Ищем блок обработки кика античита и убеждаемся, что там нет бана.
        start = src.find("kick_code = anticheat_kick(title, clean_info)")
        self.assertGreater(start, 0, "обработчик кика античита пропал")
        # Блок заканчивается на первом return после паузы — дальше идёт
        # обработка обычных банов, её не трогаем.
        pause = src.find("scheduled_reconnect_delay", start)
        self.assertGreater(pause, start, "кик античита не уходит в паузу")
        end = src.find("return", pause)
        self.assertGreater(end, pause)
        # Комментарии не в счёт: в них слово упомянуто как история.
        code = "\n".join(
            line for line in src[start:end].splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("mark_bot_banned", code,
                         "кик античита снова перманентно банит аккаунт")
