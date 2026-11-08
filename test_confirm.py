"""Подтверждение опасных действий в Telegram.

Раньше «⏹ Всех» и «❌ Удалить бота» срабатывали сразу: один случайный
тап гасил весь ботнет или стирал аккаунт из реестра без возможности
отменить. Теперь оба действия сначала спрашивают.
"""

import threading
import unittest
from unittest import mock

import bot as botmod
import tg_controller as tg


class FakeBot:
    def __init__(self, bot_id=1, nickname="Sotka_NaCapte"):
        self.bot_id = bot_id
        self.nickname = nickname
        self.status = "В игре (заспавнен)"
        self.server_name = "VORONEZH"
        self.host = "80.66.82.121"
        self.port = 5125
        self.password = "pass123"
        self.proxy = ""
        self.is_banned = False
        self.target_lvl = None
        self.default_task_name = "lvl"
        self.active_task = None
        self.queue_mode = False
        self._reconnect_pending = False

    def get_level(self):
        return "3 lvl"

    def get_balance_str(self):
        return "1500"

    def add_log(self, text):
        pass


class FakeTeleBot:
    """Мини-замена telebot.TeleBot: только регистрация и отправка."""

    def __init__(self):
        self.callback_query_handlers = []
        self.message_handlers = []
        self.sent = []
        self.edits = []

    def callback_query_handler(self, func=None, **kwargs):
        def decorator(handler):
            self.callback_query_handlers.append(
                {"function": handler, "filters": {"func": func}})
            return handler
        return decorator

    def message_handler(self, *args, **kwargs):
        def decorator(handler):
            self.message_handlers.append({"function": handler})
            return handler
        return decorator

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return mock.Mock(message_id=len(self.sent))

    def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        return True

    def answer_callback_query(self, *args, **kwargs):
        return True

    def reply_to(self, message, text, **kwargs):
        self.sent.append(text)
        return mock.Mock(message_id=len(self.sent))


class FakeCall:
    def __init__(self, data, chat_id=1, message_id=10):
        self.data = data
        self.id = "cb-1"
        self.from_user = mock.Mock(id=1)
        self.message = mock.Mock()
        self.message.chat.id = chat_id
        self.message.message_id = message_id


class ConfirmTests(unittest.TestCase):
    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = threading.RLock()
        self.manager.bots = {1: FakeBot(1), 2: FakeBot(2, "Morgenshtern_Herobrin")}
        self.manager.settings = None
        self.manager._get_bot_location_name = lambda b: ""
        self.deleted = []
        self.manager.delete_bot = lambda bid: (
            self.deleted.append(bid), self.manager.bots.pop(bid, None))
        self.stopped = []
        self.manager.stop_all = lambda: self.stopped.append(True)

        self.ctrl = tg.TelegramBotController.__new__(tg.TelegramBotController)
        self.ctrl.token = "1:a"
        self.ctrl.manager = self.manager
        self.ctrl.admin_ids = [1]
        self.ctrl.admin_id = 1
        self.ctrl.active_chats = {1}
        self.ctrl.user_states = {}
        self.ctrl._live_views = {}
        self.ctrl._views_lock = threading.RLock()
        self.ctrl._live_thread = None
        self.ctrl.running = True
        self.ctrl.ai = None
        self.ctrl.bot = FakeTeleBot()
        self.ctrl._setup_handlers()

        self.handler = None
        for entry in self.ctrl.bot.callback_query_handlers:
            func = entry.get("function")
            if func is not None:
                self.handler = func
        self.assertIsNotNone(self.handler, "обработчик callback не зарегистрирован")

    @property
    def sent(self):
        return self.ctrl.bot.sent

    def press(self, data):
        self.sent.clear()
        self.handler(FakeCall(data))

    # --- остановить всех ---

    def test_stopall_asks_first(self):
        self.press("act_stopall")
        self.assertEqual(self.stopped, [], "боты остановлены без подтверждения")
        self.assertTrue(self.sent, "не показан вопрос")
        self.assertIn("Остановить всех", self.sent[0])

    def test_stopall_shows_counts(self):
        self.press("act_stopall")
        self.assertIn("2", self.sent[0])

    def test_stopall_confirm_actually_stops(self):
        self.press("act_stopall_confirm")
        self.assertEqual(self.stopped, [True])

    def test_stopall_cancel_does_not_stop(self):
        self.press("act_stopall_cancel")
        self.assertEqual(self.stopped, [])

    # --- удаление бота ---

    def test_delete_asks_first(self):
        self.press("bot_del:1:1")
        self.assertEqual(self.deleted, [], "бот удалён без подтверждения")
        self.assertTrue(self.sent)
        self.assertIn("Удалить бота #1", self.sent[0])

    def test_delete_shows_nickname(self):
        self.press("bot_del:1:1")
        self.assertIn("Sotka_NaCapte", self.sent[0])

    def test_delete_confirm_actually_deletes(self):
        self.press("bot_del_confirm:1:1")
        self.assertEqual(self.deleted, [1])

    def test_delete_of_missing_bot_is_safe(self):
        self.press("bot_del:999:1")
        self.assertEqual(self.deleted, [])

    def test_delete_cancel_does_not_delete(self):
        """Отмена возвращает в карточку, а не удаляет."""
        self.press("bot_view:1:1")
        self.assertEqual(self.deleted, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
