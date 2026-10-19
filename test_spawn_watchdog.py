"""Интеграционные тесты сторожа спавна на имитации сервера.

Новое поведение сторожа (после отказа от RPC-хендшейка): он НЕ зовёт
force_spawn и не шлёт request_class + spawn — именно этот хендшейк и был
причиной кика античитом Black Russia кодом 25. Теперь сторож только ждёт
меню вокзалов до MENU_WAIT, отвечает на него через select_spawn и
провожает бота до вокзала. Если меню не пришло — пишет
report = {"status": "no_menu", ...} и выходит из цикла.

Запуск: python3 -m unittest test_spawn_watchdog -v
"""
import threading
import time
import unittest
from unittest import mock

from samp.constants import PacketPriority, PacketReliability
from samp.packets import PacketBuilder

from logic import spawn as S

RC_PKT = PacketBuilder.rpc_build("request_class", 0)
SP_PKT = PacketBuilder.rpc_build("spawn")

AUTH = (13.703383445739746, 485.29071044921875, -20.0)
SKY = (1469.6, 2495.1, 2501.0)
STATION = (2742.1, -2445.2, 21.8)
MENU = {"o": 1, "m": [1, 2]}


class FakeSync:
    def __init__(self):
        self.enabled = False

    def start(self):
        self.enabled = True


class FakeApi:
    def __init__(self, pos):
        self.pos = pos

    def getbotposition(self):
        return self.pos


class FakeServerProtocol:
    """Сервер: 372 в небо на первый request_class, 448 на вокзал на второй.

    Сторож эти RPC больше не шлёт — протокол нужен, чтобы поймать
    регрессию: если хендшейк вернётся, в sent появятся пакеты, а бот
    уедет в небо.
    """

    def __init__(self, api, *, answer_ordered=True, delay=0.0):
        self.api = api
        self.answer_ordered = answer_ordered
        self.delay = delay
        self.sent = []
        self._lock = threading.Lock()

    def send_with_header(self, payload, reliability, ordering_channel=0, priority=0):
        with self._lock:
            self.sent.append((payload, reliability, priority))
        if payload == RC_PKT and reliability == PacketReliability.RELIABLE:
            if self.delay:
                time.sleep(self.delay)
            self.api.pos = SKY
        elif payload == RC_PKT and reliability == PacketReliability.RELIABLE_ORDERED:
            if self.answer_ordered:
                if self.delay:
                    time.sleep(self.delay)
                self.api.pos = STATION

    def count(self, payload):
        with self._lock:
            return sum(1 for p, _, _ in self.sent if p == payload)


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
    def __init__(self, pos=AUTH, *, answer_ordered=True, delay=0.0):
        self.api = FakeApi(pos)
        self.protocol = FakeServerProtocol(self.api, answer_ordered=answer_ordered, delay=delay)
        self.session = FakeSession(self.api, self.protocol)
        self.marked = []
        self.logs = []
        self.status = "В игре (авторизован)"

    def mark_spawned(self, position=None):
        self.marked.append(position or self.api.pos)
        self.status = "В игре (заспавнен)"
        return True

    def log(self, msg):
        self.logs.append(msg)

    def teleport_to_station_on_select(self):
        """Сервер высаживает на вокзал в ответ на SPAWN_SELECT."""
        self.session.on_spawn_select = lambda iid, payload: setattr(self.api, "pos", STATION)


class WatchdogIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Человеческая пауза перед ответом на меню (1.6-3.4с) проверяется
        # отдельно в test_spawn_unit. Здесь важна логика, а не тайминг.
        patcher = mock.patch.object(S, "spawn_answer_delay", lambda rng=None: 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_for(self, predicate, timeout=8.0, poll=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def test_watchdog_brings_bot_to_station(self):
        """Меню пришло на ходу — сторож дождался, ответил и довёл до вокзала."""
        bot = FakeBot()
        bot.teleport_to_station_on_select()

        def server_sends_menu():
            time.sleep(0.15)
            S.remember_spawn_menu(MENU, bot)

        threading.Thread(target=server_sends_menu, daemon=True).start()

        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=1.0, stable_seconds=0.0, menu_wait=3.0
        )
        wd.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: len(bot.marked) == 1),
                "бот не доехал до вокзала",
            )
            self.assertEqual(bot.marked, [STATION])
            self.assertEqual(bot.status, "В игре (заспавнен)")
            self.assertTrue(bot.session.sync.enabled)
            self.assertEqual(wd.report["status"], "spawned")
            self.assertEqual(wd.report["position"], STATION)
            # Ответ на меню ушёл один раз и это SPAWN_SELECT (iid=50).
            self.assertEqual(bot.session.json_sent, [(50, {"t": 1, "s": 1})])
            # И ни одного RPC-хендшейка.
            self.assertEqual(bot.protocol.sent, [], "сторож отправил RPC")
            self.assertFalse(wd.running)
        finally:
            wd.stop()
            S.reset_menu(bot)

    def test_watchdog_gives_up_when_server_silent(self):
        """Меню не пришло — no_menu, сторож встаёт и хендшейк не трогает."""
        bot = FakeBot()
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=0.3, stable_seconds=0.0, menu_wait=0.3
        )
        wd.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: not wd.running, timeout=5.0),
                "сторож не остановился, хотя меню не пришло",
            )
            self.assertEqual(bot.marked, [])
            self.assertEqual(bot.status, "В игре (авторизован)")
            self.assertEqual(wd.report["status"], "no_menu")
            self.assertEqual(wd.report["reason"], "меню вокзалов не пришло")
            self.assertEqual(wd.report["position"], AUTH)
            self.assertTrue(S.in_auth_zone(bot.api.pos))
            self.assertFalse(S.in_sky(bot.api.pos), "сторож ушёл в небо хендшейком")
            self.assertEqual(bot.protocol.sent, [], "сторож отправил RPC-хендшейк")
            self.assertEqual(bot.session.json_sent, [], "отвечал на меню, которого не было")
            self.assertTrue(
                any("меню не пришло" in msg for msg in bot.logs),
                f"в логе нет причины отказа: {bot.logs}",
            )
        finally:
            wd.stop()
            S.reset_menu(bot)

    def test_watchdog_uses_menu_when_server_sends_one(self):
        """Меню пришло до старта сторожа — он отвечает на него, а не молчит."""
        bot = FakeBot()
        bot.teleport_to_station_on_select()
        S.remember_spawn_menu(MENU, bot)
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=2.0, stable_seconds=0.0, menu_wait=0.5
        )
        wd.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: len(bot.marked) == 1),
                "меню не сработало",
            )
            self.assertEqual(bot.session.json_sent, [(50, {"t": 1, "s": 1})])
            self.assertEqual(wd.report["status"], "spawned")
            self.assertEqual(bot.protocol.sent, [])
        finally:
            wd.stop()
            S.reset_menu(bot)

    def test_watchdog_does_nothing_when_already_spawned(self):
        bot = FakeBot(pos=STATION)
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=0.5, stable_seconds=0.0, menu_wait=0.0
        )
        wd.start()
        try:
            self.assertTrue(self._wait_for(lambda: len(bot.marked) == 1))
            self.assertEqual(bot.protocol.sent, [])
            self.assertEqual(bot.marked, [STATION])
            self.assertEqual(wd.report["status"], "spawned")
        finally:
            wd.stop()

    def test_watchdog_stops_when_session_drops(self):
        bot = FakeBot()
        bot.session.connected = False
        wd = S.SpawnWatchdog(bot, log=bot.log, menu_wait=0.0)
        wd.OFFLINE_GRACE = 0.2
        wd.start()
        self.assertTrue(self._wait_for(lambda: not wd.running, timeout=3.0))

    def test_watchdog_stop_is_clean(self):
        bot = FakeBot()
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=5.0, stable_seconds=0.0, menu_wait=2.0
        )
        wd.start()
        time.sleep(0.3)
        wd.stop()
        self.assertTrue(self._wait_for(lambda: not wd.running, timeout=3.0))
        if wd._thread is not None:
            wd._thread.join(timeout=3.0)
        self.assertFalse(wd._thread.is_alive(), "поток сторожа остался висеть")

    def test_delivery_is_split_between_steps(self):
        """force_spawn сам по себе остался рабочим — тест на его доставку.

        В проде хендшейк выключен (ALLOW_RPC_SPAWN_HANDSHAKE = False),
        поэтому включаем его только на время теста.
        """
        bot = FakeBot()
        orig = S.ALLOW_RPC_SPAWN_HANDSHAKE
        S.ALLOW_RPC_SPAWN_HANDSHAKE = True
        try:
            S.force_spawn(bot, log=bot.log, step_delay=0.0, timeout=1.0,
                          stable_seconds=0.0)
        finally:
            S.ALLOW_RPC_SPAWN_HANDSHAKE = orig
        rels = [(p, r, pr) for p, r, pr in bot.protocol.sent]
        self.assertEqual(rels[0], (RC_PKT, PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY))
        self.assertEqual(rels[1], (SP_PKT, PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY))
        self.assertEqual(rels[2], (RC_PKT, PacketReliability.RELIABLE_ORDERED, PacketPriority.MEDIUM_PRIORITY))


class NoHandshakeRegressionTests(unittest.TestCase):
    """Регрессия на кик античитом кодом 25.

    RPC request_class + spawn при открытом меню вокзалов — это кик. Сторож
    не должен звать force_spawn ни в одном сценарии: ни когда меню пришло,
    ни когда сервер молчит.
    """

    def setUp(self):
        patcher = mock.patch.object(S, "spawn_answer_delay", lambda rng=None: 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_watchdog(self, bot, *, menu_wait, send_menu, timeout=6.0):
        if send_menu:
            def later():
                time.sleep(0.1)
                S.remember_spawn_menu(MENU, bot)

            threading.Thread(target=later, daemon=True).start()

        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=0.5, stable_seconds=0.0, menu_wait=menu_wait
        )
        wd.start()
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and wd.report is None:
                time.sleep(0.02)
            wd.stop()
            if wd._thread is not None:
                wd._thread.join(timeout=2.0)
        finally:
            wd.stop()
        return wd

    def test_watchdog_never_calls_force_spawn(self):
        for send_menu in (True, False):
            with self.subTest(menu_prishlo=send_menu):
                bot = FakeBot()
                if send_menu:
                    bot.teleport_to_station_on_select()

                with mock.patch.object(S, "force_spawn") as force_mock, \
                        mock.patch.object(S, "request_class", wraps=S.request_class) as rc_mock, \
                        mock.patch.object(S, "send_spawn", wraps=S.send_spawn) as sp_mock:
                    wd = self._run_watchdog(bot, menu_wait=1.0, send_menu=send_menu)

                    self.assertEqual(
                        force_mock.call_count, 0,
                        "сторож позвал force_spawn — это и есть кик кодом 25",
                    )
                    self.assertEqual(
                        rc_mock.call_count, 0, "сторож отправил request_class"
                    )
                    self.assertEqual(
                        sp_mock.call_count, 0, "сторож отправил spawn"
                    )
                    self.assertEqual(
                        bot.protocol.count(RC_PKT), 0, "в сокет ушёл request_class"
                    )
                    self.assertEqual(
                        bot.protocol.count(SP_PKT), 0, "в сокет ушёл spawn"
                    )
                    self.assertEqual(bot.protocol.sent, [])

                    # Тест не должен быть пустым: сценарий реально отработал.
                    expected = "spawned" if send_menu else "no_menu"
                    self.assertEqual(wd.report["status"], expected)

                S.reset_menu(bot)


if __name__ == "__main__":
    unittest.main()
