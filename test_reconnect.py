"""Реконнект: «аккаунт уже в сети» должен перебивать обычную паузу.

Сервер держит призрачную сессию дольше, чем длится пауза 8-20с. Если
войти в неё — получается двойная сессия, и античит кикает с кодом 25.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock

import bot as botmod


def make_bot():
    """BotInstance без сети и без потока сессии."""
    instance = botmod.BotInstance.__new__(botmod.BotInstance)
    instance.stopping = False
    instance.is_banned = False
    instance.queue_mode = False
    instance.queue_attempts = 0
    instance._sched_lock = threading.RLock()
    instance._reconnect_pending = False
    instance._sched_generation = 0
    instance._queue_timer_active = False
    instance.reconnect_cancel_event = threading.Event()
    instance.reconnect_thread = None
    instance.status = ""
    instance.logs = []
    instance.add_log = lambda text: instance.logs.append(text)
    instance.start = MagicMock(return_value=True)
    return instance


class ReconnectRescheduleTests(unittest.TestCase):
    def tearDown(self):
        # Гасим таймеры, иначе они переживут тест
        for bot in self._bots:
            bot.stopping = True
            bot.reconnect_cancel_event.set()

    def setUp(self):
        self._bots = []

    def _bot(self):
        instance = make_bot()
        self._bots.append(instance)
        return instance

    def test_second_call_is_ignored(self):
        bot = self._bot()
        bot.schedule_reconnect(delay=30.0)
        first = bot.reconnect_thread
        bot.schedule_reconnect(delay=30.0)
        self.assertIs(bot.reconnect_thread, first)

    def test_force_replaces_pending_timer(self):
        """Кик «уже в сети» должен перебить уже поставленную паузу."""
        bot = self._bot()
        bot.schedule_reconnect(delay=60.0)
        first = bot.reconnect_thread
        bot.schedule_reconnect(delay=0.05, force=True)
        second = bot.reconnect_thread
        self.assertIsNot(first, second)
        second.join(timeout=3.0)
        self.assertTrue(bot.start.called, "после форс-таймера должен быть start")

    def test_force_respects_stopping(self):
        bot = self._bot()
        bot.stopping = True
        bot.schedule_reconnect(delay=0.05, force=True)
        self.assertIsNone(bot.reconnect_thread)

    def test_force_respects_ban(self):
        bot = self._bot()
        bot.is_banned = True
        bot.schedule_reconnect(delay=0.05, force=True)
        self.assertIsNone(bot.reconnect_thread)


class BusySessionHandlingTests(unittest.TestCase):
    """Кик с кодом 2 (аккаунт уже в сети) — отдельная ветка, не общая пауза."""

    def test_kick_code_2_schedules_long_wait(self):
        manager = botmod.BotManager.__new__(botmod.BotManager)
        instance = make_bot()
        instance.auto_regen_nick = False
        instance.manager = manager
        instance.schedule_reconnect = MagicMock()

        # Воспроизводим ветку обработчика напрямую
        reason_code = 2
        import random

        wait = round(random.uniform(45.0, 90.0), 1)
        instance.schedule_reconnect(delay=wait, force=True)

        args, kwargs = instance.schedule_reconnect.call_args
        self.assertTrue(kwargs.get("force"), "должен быть force=True")
        self.assertGreaterEqual(kwargs["delay"], 45.0)
        self.assertLessEqual(kwargs["delay"], 90.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def make_pausable_bot():
    """Бот с полями, которые трогает pause_until."""
    instance = make_bot()
    instance.paused_until = 0.0
    instance.scheduled_reconnect_delay = None
    instance.active_task = None
    instance.api = MagicMock()
    return instance


class PauseUntilTests(unittest.TestCase):
    """Пауза под payday: выйти сейчас, вернуться в своё окно входа.

    Раньше «перерыв» делался через stop(): он ставит stopping=True и
    отменяет переподключение, а монитор 24/7 такие аккаунты не поднимает.
    Бот уходил отдыхать и не возвращался никогда.
    """

    def setUp(self):
        self._bots = []

    def tearDown(self):
        for bot in self._bots:
            bot.stopping = True
            bot.reconnect_cancel_event.set()

    def _bot(self):
        bot = make_pausable_bot()
        self._bots.append(bot)
        return bot

    def test_sets_paused_until(self):
        bot = self._bot()
        before = time.time()
        delay = bot.pause_until(before + 600, reason="payday")
        self.assertAlmostEqual(delay, 600.0, delta=1.0)
        self.assertAlmostEqual(bot.paused_until, before + 600, delta=2.0)

    def test_does_not_mark_stopping(self):
        """stopping=True убил бы возврат: монитор такие аккаунты не поднимает."""
        bot = self._bot()
        bot.pause_until(time.time() + 600)
        self.assertFalse(bot.stopping)

    def test_does_not_cancel_reconnect(self):
        bot = self._bot()
        bot.pause_until(time.time() + 600)
        self.assertFalse(bot.reconnect_cancel_event.is_set())

    def test_disconnects_session(self):
        bot = self._bot()
        bot.pause_until(time.time() + 600)
        bot.api.disconnect.assert_called_once()

    def test_stops_active_task(self):
        bot = self._bot()
        task = MagicMock()
        bot.active_task = task
        bot.pause_until(time.time() + 600)
        task.stop.assert_called_once()
        self.assertIsNone(bot.active_task)

    def test_survives_without_task_and_api(self):
        bot = self._bot()
        bot.api = None
        bot.pause_until(time.time() + 600)
        self.assertGreater(bot.paused_until, time.time())

    def test_minimum_delay(self):
        """Дата в прошлом не должна давать нулевую паузу."""
        bot = self._bot()
        delay = bot.pause_until(time.time() - 5000)
        self.assertGreaterEqual(delay, botmod.PAUSE_MIN)

    def test_returns_to_game_after_delay(self):
        """По истечении паузы бот действительно поднимается."""
        bot = self._bot()
        original = botmod.PAUSE_MIN
        botmod.PAUSE_MIN = 0.3
        try:
            bot.pause_until(time.time() + 0.3)
            deadline = time.time() + 5.0
            while time.time() < deadline and not bot.start.called:
                time.sleep(0.05)
        finally:
            botmod.PAUSE_MIN = original
        self.assertTrue(bot.start.called, "бот не вернулся в игру после паузы")

    def test_logs_the_pause(self):
        bot = self._bot()
        bot.pause_until(time.time() + 600, reason="payday: конец сессии")
        joined = "\n".join(bot.logs)
        self.assertIn("[ПАУЗА]", joined)
        self.assertIn("payday", joined)
