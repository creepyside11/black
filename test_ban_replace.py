"""Автозамена забаненных аккаунтов.

Раньше замена срабатывала только если в тексте бана встречалось слово из
узкого списка: «бот», «чит», «кликер», «постороннее ПО». Сервер в
большинстве случаев пишет другое — «автобан сервера (без админа и
причины)» или «Ваш игровой аккаунт заблокирован, вход невозможен», — и
замена не создавалась. Из 105 банов в логе замену получили 16.
"""

import threading
import time
import unittest
from unittest import mock

import bot as botmod


class FakeBot:
    def __init__(self, bot_id=1, nickname="Sotka_NaCapte", server="NOVGOROD"):
        self.bot_id = bot_id
        self.nickname = nickname
        self.server_name = server
        self.password = "pass123"
        self.default_task_name = "lvl"
        self.proxy = ""
        self.status = "В игре (заспавнен)"
        self.is_banned = False
        self.banned_at = 0.0
        self.ban_admin = ""
        self.ban_reason = ""
        self.ban_kind = ""
        self.ban_duration = ""
        self.stopping = False
        self.logs = []

    def add_log(self, text):
        self.logs.append(text)

    def stop(self, banned=False):
        self.status = "ЗАБАНЕН" if banned else "Остановлен"

    def cancel_reconnect(self):
        pass


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = threading.Lock()
        self.manager.bots = {}
        self.manager.settings = None
        self.manager.tg_controller = None
        self.manager.accounts_file = "/dev/null"
        self.manager.save_accounts = lambda: None
        self.manager._replace_lock = threading.Lock()
        self.manager._replace_recent = []

        self.created = []
        self.manager.add_bot = lambda **kw: (
            self.created.append(kw) or mock.Mock(bot_id=99, nickname="New_Nick",
                                                 default_task_name="lvl",
                                                 start=lambda: None))

        # Троттлинг в тестах не должен спать по-настоящему.
        self._patches = [
            mock.patch.object(botmod, "REPLACE_INTERVAL", 0.0),
            mock.patch.object(botmod, "REPLACE_FIRST_DELAY", (0.0, 0.0)),
            mock.patch.object(botmod, "REPLACE_PER_HOUR", 20),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def ban(self, reason, raw=""):
        b = FakeBot()
        self.manager.mark_bot_banned(b, reason=reason, raw=raw)
        # Замена идёт в отдельном потоке — ждём её.
        for _ in range(200):
            if self.created:
                break
            time.sleep(0.01)
        return b

    # --- причина больше не важна ---

    def test_auto_ban_triggers_replacement(self):
        """«автобан сервера (без админа и причины)» — раньше не заменялся."""
        self.ban("автобан сервера (без админа и причины)")
        self.assertEqual(len(self.created), 1)

    def test_blocked_account_message_triggers_replacement(self):
        """«Ваш игровой аккаунт заблокирован, вход невозможен»."""
        self.ban("Ваш игровой аккаунт заблокирован, вход невозможен.")
        self.assertEqual(len(self.created), 1)

    def test_anticheat_ban_triggers_replacement(self):
        self.ban("Античит, код 25")
        self.assertEqual(len(self.created), 1)

    def test_admin_bot_ban_triggers_replacement(self):
        self.ban("Чат: Администратор Maks_Almazov навсегда заблокировал "
                 "игрока Sharay_Makuh. Причина: BOT")
        self.assertEqual(len(self.created), 1)

    def test_automation_ban_triggers_replacement(self):
        self.ban("за автоматизация) · навсегда")
        self.assertEqual(len(self.created), 1)

    # --- исключение: IP-бан ---

    def test_connection_ban_does_not_replace(self):
        """IP-бан: новый аккаунт с того же адреса забанят так же."""
        self.ban("Пакет CONNECTION_BANNED (0x19)")
        self.assertEqual(self.created, [])

    # --- защита от повторов и лавины ---

    def test_second_call_does_not_replace_twice(self):
        b = FakeBot()
        self.manager.mark_bot_banned(b, reason="автобан сервера")
        for _ in range(200):
            if self.created:
                break
            time.sleep(0.01)
        self.assertEqual(len(self.created), 1)
        # Тот же бот, тот же бан — второй раз не заменяем.
        self.manager.mark_bot_banned(b, reason="автобан сервера")
        time.sleep(0.15)
        self.assertEqual(len(self.created), 1)

    def test_hourly_limit_caps_replacements(self):
        with mock.patch.object(botmod, "REPLACE_PER_HOUR", 3):
            self.manager._replace_recent = []
            for i in range(6):
                b = FakeBot(bot_id=100 + i)
                self.manager.mark_bot_banned(b, reason="автобан сервера")
            for _ in range(500):
                if len(self.created) >= 3:
                    break
                time.sleep(0.01)
            time.sleep(0.3)
        self.assertEqual(len(self.created), 3)

    def test_replacement_uses_same_server(self):
        self.ban("автобан сервера")
        self.assertEqual(self.created[0]["target_server"], "NOVGOROD")

    def test_replacement_keeps_password(self):
        self.ban("автобан сервера")
        self.assertEqual(self.created[0]["password"], "pass123")

    def test_replacement_asks_for_generated_nick(self):
        self.ban("автобан сервера")
        self.assertIsNone(self.created[0]["nickname"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
