"""Бан не создаёт новые аккаунты и не удаляет исходную запись."""
import threading
import unittest
from unittest import mock
import bot as botmod

class FakeBot:
    def __init__(self):
        self.bot_id=1;self.nickname='Test_Account';self.server_name='NOVGOROD'
        self.password='example';self.default_task_name='lvl';self.proxy=''
        self.status='В игре';self.is_banned=False;self.banned_at=0.0
        self.logs=[];self.stopping=False
    def add_log(self,text):self.logs.append(text)
    def stop(self,banned=False):self.status='ЗАБАНЕН' if banned else 'Остановлен'
    def cancel_reconnect(self):pass

class BanKeepsAccountTests(unittest.TestCase):
    def setUp(self):
        self.manager=botmod.BotManager.__new__(botmod.BotManager)
        self.bot=FakeBot();self.manager.bots={1:self.bot};self.manager._lock=threading.Lock()
        self.manager.tg_controller=None;self.manager.save_accounts=mock.Mock();self.manager.add_bot=mock.Mock()
        self.manager.handle_ip_ban=mock.Mock()
    def test_account_ban_keeps_original_and_never_creates_new_one(self):
        self.manager.mark_bot_banned(self.bot,reason='Ваш игровой аккаунт заблокирован')
        self.assertTrue(self.bot.is_banned);self.assertIs(self.manager.bots[1],self.bot)
        self.assertEqual(self.bot.nickname,'Test_Account');self.manager.add_bot.assert_not_called()
        self.manager.save_accounts.assert_called_once()
    def test_duplicate_notice_does_not_duplicate_save_or_create(self):
        self.manager.mark_bot_banned(self.bot,reason='Ваш игровой аккаунт заблокирован')
        self.manager.mark_bot_banned(self.bot,reason='Ваш игровой аккаунт заблокирован')
        self.manager.save_accounts.assert_called_once();self.manager.add_bot.assert_not_called()
    def test_address_ban_does_not_mark_account(self):
        self.manager.mark_bot_banned(self.bot,reason='CONNECTION_BANNED (0x19)')
        self.assertFalse(self.bot.is_banned);self.manager.handle_ip_ban.assert_called_once()
        self.manager.add_bot.assert_not_called();self.manager.save_accounts.assert_not_called()
