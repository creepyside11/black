"""Мастер массовой регистрации: сервер -> количество -> пароль -> задача.

Раньше всё вводилось одной строкой «TVER 5 пароль lvl». С телефона это
мучение: надо помнить порядок полей и точное имя сервера.
"""

import unittest
from unittest import mock

import bot as botmod
import tg_controller as tg


class FakeMessage:
    def __init__(self, text="", chat_id=1, message_id=10):
        self.text = text
        self.chat = type("Chat", (), {"id": chat_id})()
        self.message_id = message_id
        self.from_user = type("User", (), {"id": 8282599467})()


class ControllerHarness(unittest.TestCase):
    """Контроллер без сети: подменяем всё, что лезет в Telegram."""

    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = __import__("threading").RLock()
        self.manager.bots = {}
        self.manager._nick_index = {}
        self.manager._obj_index = {}
        self.manager.active_bot_id = None
        self.manager.settings = None
        self.manager._get_bot_location_name = lambda b: ""

        self.sent = []
        self.edited = []
        self.answered = []

        self.ctrl = tg.TelegramBotController.__new__(tg.TelegramBotController)
        self.ctrl.token = "123:abc"
        self.ctrl.manager = self.manager
        self.ctrl.admin_ids = [8282599467]
        self.ctrl.admin_id = 8282599467
        self.ctrl.active_chats = set(self.ctrl.admin_ids)
        self.ctrl.user_states = {}
        self.ctrl._live_views = {}
        self.ctrl._views_lock = __import__("threading").RLock()
        self.ctrl._live_thread = None
        self.ctrl.running = False
        self.ctrl.bot = mock.Mock()
        self.ctrl.bot.send_message.side_effect = self._on_send
        self.ctrl.bot.edit_message_text.side_effect = self._on_edit
        self.ctrl.bot.answer_callback_query.side_effect = (
            lambda cid, *a, **k: self.answered.append((cid, a, k)))
        self.ctrl.bot.delete_message.side_effect = lambda *a, **k: None

    def _on_send(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append((chat_id, text, reply_markup))
        return type("Sent", (), {"message_id": len(self.sent)})()

    def _on_edit(self, text, chat_id=None, message_id=None, reply_markup=None, **kwargs):
        self.edited.append((chat_id, message_id, text, reply_markup))

    @property
    def last_text(self):
        return self.sent[-1][1] if self.sent else ""

    @property
    def last_markup(self):
        return self.sent[-1][2] if self.sent else None


class ServerStepTests(ControllerHarness):
    def test_first_step_lists_servers_with_pagination(self):
        self.ctrl._render_batch_servers(1, 1)
        self.assertIn("шаг 1 из 5", self.last_text)
        buttons = self._buttons()
        self.assertTrue(any(b.callback_data.startswith("batch_srv:") for b in buttons))
        self.assertTrue(any(b.callback_data.startswith("batch_page:") for b in buttons))

    def _buttons(self):
        markup = self.last_markup
        return [b for row in markup.keyboard for b in row]

    def test_pagination_moves(self):
        self.ctrl._render_batch_servers(1, 2)
        self.assertIn("страница <b>2</b>", self.last_text)

    def test_page_clamped(self):
        self.ctrl._render_batch_servers(1, 9999)
        self.assertIn("страница <b>", self.last_text)

    def test_servers_have_load_marks(self):
        self.ctrl._render_batch_servers(1, 1)
        labels = " ".join(b.text for b in self._buttons())
        self.assertTrue(any(m in labels for m in ("🟢", "🟡", "🔴")))

    def test_cancel_button_present(self):
        self.ctrl._render_batch_servers(1, 1)
        self.assertTrue(any(b.callback_data == "batch_cancel" for b in self._buttons()))


class WizardFlowTests(ControllerHarness):
    def setUp(self):
        super().setUp()
        from logic.settings import Settings
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.manager.settings = Settings(self._tmp.name)
        self._settings_path = self._tmp.name

    def tearDown(self):
        import os
        try:
            os.unlink(self._settings_path)
        except OSError:
            pass

    def test_count_step_asks_number(self):
        self.ctrl._ask_batch_count(1, "TOLYATTI")
        self.assertIn("шаг 2 из 5", self.last_text)
        self.assertIn("TOLYATTI", self.last_text)
        self.assertEqual(self.ctrl.user_states[1]["action"], "batch_count")

    def test_password_step_offers_base_password(self):
        self.ctrl._ask_batch_password(1, "TOLYATTI", 5)
        self.assertIn("шаг 3 из 5", self.last_text)
        buttons = [b for row in self.last_markup.keyboard for b in row]
        self.assertTrue(any(b.callback_data == "batch_pwd_base" for b in buttons))

    def test_password_step_without_base_has_no_base_button(self):
        self.manager.settings.base_password = ""
        self.ctrl._ask_batch_password(1, "TOLYATTI", 5)
        buttons = [b for row in self.last_markup.keyboard for b in row]
        self.assertFalse(any(b.callback_data == "batch_pwd_base" for b in buttons))

    def test_task_step_lists_tasks(self):
        self.ctrl._ask_batch_task(1, "TOLYATTI", 5, "pass123")
        self.assertIn("шаг 4 из 5", self.last_text)
        buttons = [b for row in self.last_markup.keyboard for b in row]
        tasks = {b.callback_data for b in buttons if b.callback_data.startswith("batch_task:")}
        self.assertEqual(len(tasks), 7)
        self.assertIn("batch_task:lvl", tasks)
        self.assertIn("batch_task:farm", tasks)

    def test_full_chain_state(self):
        """Состояние проносится через все шаги."""
        self.ctrl.user_states[1] = {"action": "batch_count", "server": "TOLYATTI"}
        self.ctrl._ask_batch_password(1, "TOLYATTI", 3)
        self.assertEqual(self.ctrl.user_states[1]["count"], 3)
        self.assertEqual(self.ctrl.user_states[1]["server"], "TOLYATTI")
        self.ctrl._ask_batch_task(1, "TOLYATTI", 3, "pass123")
        self.assertEqual(self.ctrl.user_states[1]["password"], "pass123")

    def test_run_batch_rejects_over_limit(self):
        self.manager.settings.batch_limit = 5
        with mock.patch.object(self.manager, "add_bots_batch") as batch:
            self.ctrl._run_batch(1, "TOLYATTI", 100, "pass123", "lvl")
        batch.assert_not_called()
        self.assertIn("Лимит партии", self.last_text)


class SettingsMenuTests(ControllerHarness):
    def setUp(self):
        super().setUp()
        from logic.settings import Settings
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.manager.settings = Settings(self._tmp.name)
        self._settings_path = self._tmp.name

    def tearDown(self):
        import os
        try:
            os.unlink(self._settings_path)
        except OSError:
            pass

    def test_menu_shows_base_password(self):
        self.manager.settings.base_password = "mybasepass"
        self.ctrl._render_settings(1)
        self.assertIn("mybasepass", self.last_text)

    def test_menu_has_all_controls(self):
        self.ctrl._render_settings(1)
        buttons = [b for row in self.last_markup.keyboard for b in row]
        callbacks = {b.callback_data for b in buttons}
        self.assertIn("set_base_password", callbacks)
        self.assertIn("set_default_task", callbacks)
        self.assertIn("set_batch_limit", callbacks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
