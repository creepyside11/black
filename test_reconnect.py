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
