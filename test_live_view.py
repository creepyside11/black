"""Автообновление окон Telegram.

Раньше статус менялся только по кнопке «Обновить». Запустил аккаунт,
он вошёл — а в окне всё ещё «Ожидание», пока сам не перезайдёшь в меню.
Теперь окна запоминаются, фоновая нить сравнивает подпись содержимого и
перерисовывает только изменившиеся.
"""

import threading
import unittest
from unittest import mock

import bot as botmod
import tg_controller as tg


class FakeBot:
    """Заглушка BotInstance: только то, что читает карточка."""

    def __init__(self, bot_id=1, nickname="Test_Nick", status="Ожидание"):
        self.bot_id = bot_id
        self.nickname = nickname
        self.status = status
        self.server_name = "TOLYATTI"
        self.host = "80.66.82.72"
        self.port = 5125
        self.password = "pass123"
        self.proxy = ""
        self.is_banned = False
        self.target_lvl = None
        self.default_task_name = "lvl"
        self.active_task = None
        self.queue_mode = False
        self._level = 1
        self._balance = 0

    def get_level(self):
        return self._level

    def get_balance_str(self):
        return f"{self._balance}$"


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = threading.RLock()
        self.manager.bots = {1: FakeBot(1)}
        self.manager.settings = None
        self.manager._get_bot_location_name = lambda b: ""

        self.edits = []
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
        self.ctrl.bot = mock.Mock()
        self.ctrl.bot.edit_message_text.side_effect = (
            lambda text, **kw: self.edits.append(text))

    def test_view_remembers(self):
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.assertIn((1, 10), self.ctrl._live_views)

    def test_no_message_id_is_ignored(self):
        self.ctrl._remember_view(1, None, "card", bid=1)
        self.assertEqual(self.ctrl._live_views, {})

    def test_unchanged_view_is_not_redrawn(self):
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.ctrl._refresh_live_views()
        self.assertEqual(self.edits, [])

    def test_status_change_redraws(self):
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.manager.bots[1].status = "В игре (заспавнен)"
        self.ctrl._refresh_live_views()
        self.assertEqual(len(self.edits), 1)
        # Бейдж статуса рисуется как «🟢 Онлайн», а не сырым текстом.
        self.assertIn("🟢 Онлайн", self.edits[0])

    def test_level_change_redraws(self):
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.manager.bots[1]._level = 7
        self.ctrl._refresh_live_views()
        self.assertEqual(len(self.edits), 1)

    def test_second_refresh_is_quiet(self):
        """После перерисовки подпись обновилась — второй раз не дёргаем."""
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.manager.bots[1].status = "В игре"
        self.ctrl._refresh_live_views()
        self.ctrl._refresh_live_views()
        self.assertEqual(len(self.edits), 1)

    def test_deleted_bot_forgets_view(self):
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        del self.manager.bots[1]
        self.ctrl._refresh_live_views()
        self.assertNotIn((1, 10), self.ctrl._live_views)

    def test_list_view_redraws_on_status_change(self):
        self.ctrl._remember_view(1, 20, "list", page=1)
        self.manager.bots[1].status = "В игре (заспавнен)"
        self.ctrl._refresh_live_views()
        self.assertEqual(len(self.edits), 1)

    def test_list_view_redraws_on_new_bot(self):
        self.ctrl._remember_view(1, 20, "list", page=1)
        self.manager.bots[2] = FakeBot(2, "Second_Nick")
        self.ctrl._refresh_live_views()
        self.assertEqual(len(self.edits), 1)

    def test_edit_error_drops_view(self):
        """Окно удалили в Telegram — не копим мёртвые записи."""
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.manager.bots[1].status = "В игре"
        self.ctrl.bot.edit_message_text.side_effect = Exception("message to edit not found")
        self.ctrl._refresh_live_views()
        self.assertNotIn((1, 10), self.ctrl._live_views)

    def test_not_modified_keeps_view(self):
        """«message is not modified» — норма, окно живое."""
        self.ctrl._remember_view(1, 10, "card", bid=1, page=1)
        self.manager.bots[1].status = "В игре"
        self.ctrl.bot.edit_message_text.side_effect = Exception("message is not modified")
        self.ctrl._refresh_live_views()
        self.assertIn((1, 10), self.ctrl._live_views)

    def test_forget_all_chat_views(self):
        self.ctrl._remember_view(1, 10, "card", bid=1)
        self.ctrl._remember_view(1, 11, "list")
        self.ctrl._forget_view(1)
        self.assertEqual(self.ctrl._live_views, {})

    def test_card_has_all_actions(self):
        # Статус без «ожидания»: иначе кнопка будет «Остановить».
        self.manager.bots[1].status = "Остановлен"
        text, markup = self.ctrl._bot_card(self.manager.bots[1], 1)
        callbacks = {
            b.callback_data for row in markup.keyboard for b in row
        }
        self.assertIn("bot_start:1:1", callbacks)
        self.assertIn("bot_logs:1:1", callbacks)
        self.assertIn("bot_del:1:1", callbacks)

    def test_card_shows_stop_when_in_game(self):
        self.manager.bots[1].status = "В игре (заспавнен)"
        _, markup = self.ctrl._bot_card(self.manager.bots[1], 1)
        callbacks = {b.callback_data for row in markup.keyboard for b in row}
        self.assertIn("bot_stop:1:1", callbacks)
        self.assertNotIn("bot_start:1:1", callbacks)


class NicknameNoneTests(unittest.TestCase):
    """nickname=None уезжал в accounts.json как "nickname": null.

    Автозамена забаненного аккаунта передаёт nickname=None, рассчитывая на
    генерацию, но add_bot подставлял None как есть. Запись в реестре есть,
    а бота за ней нет — и загрузить её потом нельзя.
    """

    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = threading.RLock()
        self.manager.bots = {}
        self.manager._nick_index = {}
        self.manager._obj_index = {}
        self.manager.active_bot_id = None
        self.manager._next_id = 1
        self.manager._used_nicknames = set()
        self.manager.settings = None
        self.manager.proxy_pool = None

    def _add(self, nickname, password="pass123"):
        """add_bot без сети и без записи на диск. Мок генератора — снаружи."""
        self.gen = mock.Mock(return_value="Generated_Nick")
        self.manager.generate_random_nickname = self.gen
        with mock.patch.object(self.manager, "save_accounts"), \
             mock.patch.object(self.manager, "resolve_proxy", return_value=""), \
             mock.patch("bot.BotInstance") as inst:
            inst.return_value = mock.Mock(
                bot_id=1, nickname="Generated_Nick", status="Ожидание",
                active_task=None, default_task_name="lvl")
            inst.return_value.set_task = mock.Mock()
            inst.return_value.start = mock.Mock()
            return self.manager.add_bot("TOLYATTI", nickname, password)

    def test_none_nickname_is_generated(self):
        bot = self._add(None)
        self.assertIsNotNone(bot)
        self.gen.assert_called()

    def test_empty_nickname_is_generated(self):
        bot = self._add("   ")
        self.assertIsNotNone(bot)

    def test_none_never_reaches_registry(self):
        """В реестре не должно остаться бота с None в нике."""
        with mock.patch.object(self.manager, "generate_random_nickname",
                               return_value="Generated_Nick"):
            with mock.patch.object(self.manager, "save_accounts"):
                with mock.patch("bot.BotInstance") as inst:
                    inst.return_value = mock.Mock(
                        bot_id=1, nickname="Generated_Nick", status="Ожидание",
                        active_task=None, default_task_name="lvl")
                    inst.return_value.set_task = mock.Mock()
                    inst.return_value.start = mock.Mock()
                    self.manager.add_bot("TOLYATTI", None, "pass123")
        for b in self.manager.bots.values():
            self.assertIsNotNone(b.nickname)


if __name__ == "__main__":
    unittest.main(verbosity=2)
