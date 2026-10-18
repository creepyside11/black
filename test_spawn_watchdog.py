"""Интеграционные тесты сторожа спавна на имитации сервера.

Запуск: python3 -m unittest test_spawn_watchdog -v
"""
import threading
import time
import unittest

from samp.constants import PacketPriority, PacketReliability
from samp.packets import PacketBuilder

from logic import spawn as S

RC_PKT = PacketBuilder.rpc_build("request_class", 0)
SP_PKT = PacketBuilder.rpc_build("spawn")

AUTH = (13.703383445739746, 485.29071044921875, -20.0)
SKY = (1469.6, 2495.1, 2501.0)
STATION = (2742.1, -2445.2, 21.8)


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
    """Сервер: 372 в небо на первый request_class, 448 на вокзал на второй."""

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

    def send_rpc_update_scores_and_pings(self):
        pass


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


class WatchdogIntegrationTests(unittest.TestCase):
    def _wait_for(self, predicate, timeout=8.0, poll=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(poll)
        return False

    def test_watchdog_brings_bot_to_station(self):
        bot = FakeBot()
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=1.0, stable_seconds=0.0, menu_wait=0.0
        )
        wd.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: S.is_spawned_position(bot.api.pos)),
                "бот не доехал до вокзала",
            )
            self.assertTrue(self._wait_for(lambda: len(bot.marked) == 1))
            self.assertEqual(bot.marked[0], STATION)
            self.assertEqual(bot.status, "В игре (заспавнен)")
            self.assertTrue(bot.session.sync.enabled)
        finally:
            wd.stop()

    def test_watchdog_gives_up_when_server_silent(self):
        bot = FakeBot(answer_ordered=False)
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=0.3, stable_seconds=0.0, menu_wait=0.0
        )
        wd.start()
        try:
            time.sleep(3.0)
            self.assertEqual(bot.marked, [])
            self.assertTrue(S.in_sky(bot.api.pos))
            # сервер молчит — сторож продолжает попытки, а не сдаётся молча
            self.assertGreaterEqual(bot.protocol.count(RC_PKT), 1)
            self.assertTrue(wd.running)
        finally:
            wd.stop()

    def test_watchdog_uses_menu_when_server_sends_one(self):
        """Сервер прислал меню — сторож выбирает первый вокзал, без хендшейка."""
        S.reset_menu()
        bot = FakeBot()
        answered = []

        def send_json(iid, payload, packet_name=None):
            answered.append((iid, payload))
            if payload == {"t": 1, "s": 1}:
                bot.api.pos = STATION
            return True

        bot.session.send_json = send_json
        # Меню приходит раньше, чем сторож успевает начать хендшейк.
        S.remember_spawn_menu({"o": 1, "m": [1, 2]})
        wd = S.SpawnWatchdog(
            bot, log=bot.log, attempt_timeout=2.0, stable_seconds=0.0, menu_wait=0.0
        )
        wd.start()
        try:
            self.assertTrue(
                self._wait_for(lambda: S.is_spawned_position(bot.api.pos)),
                "меню не сработало",
            )
            self.assertEqual(answered, [(50, {"t": 1, "s": 1})])
            self.assertTrue(self._wait_for(lambda: bot.marked == [STATION]))
        finally:
            wd.stop()
            S.reset_menu()

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
        finally:
            wd.stop()

    def test_watchdog_stops_when_session_drops(self):
        bot = FakeBot()
        bot.session.connected = False
        wd = S.SpawnWatchdog(bot, log=bot.log)
        wd.OFFLINE_GRACE = 0.2
        wd.start()
        self.assertTrue(self._wait_for(lambda: not wd.running, timeout=3.0))

    def test_watchdog_stop_is_clean(self):
        bot = FakeBot(answer_ordered=False)
        wd = S.SpawnWatchdog(bot, log=bot.log, attempt_timeout=5.0, stable_seconds=0.0)
        wd.start()
        time.sleep(0.5)
        wd.stop()
        self.assertTrue(self._wait_for(lambda: not wd.running, timeout=3.0))

    def test_delivery_is_split_between_steps(self):
        bot = FakeBot()
        S.force_spawn(bot, log=bot.log, step_delay=0.0, timeout=1.0, stable_seconds=0.0)
        rels = [(p, r, pr) for p, r, pr in bot.protocol.sent]
        self.assertEqual(rels[0], (RC_PKT, PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY))
        self.assertEqual(rels[1], (SP_PKT, PacketReliability.RELIABLE, PacketPriority.SYSTEM_PRIORITY))
        self.assertEqual(rels[2], (RC_PKT, PacketReliability.RELIABLE_ORDERED, PacketPriority.MEDIUM_PRIORITY))


if __name__ == "__main__":
    unittest.main()
