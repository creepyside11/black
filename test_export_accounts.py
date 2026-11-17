"""Export remains private and never changes running accounts."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import tg_controller as tg
from test_telegram_lua import FakeTelegram


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.bot = FakeTelegram()
        self.documents = []
        def send_document(chat, doc, **kwargs):
            self.documents.append((chat, doc.name, doc.read().decode('utf-8'), kwargs))
        self.bot.send_document = mock.Mock(side_effect=send_document)
        self.account = NS(nickname='Test_User', password='pass:word', server_name='ARZAMAS', stop=mock.Mock())
        self.controller = tg.TelegramBotController.__new__(tg.TelegramBotController)
        self.controller.admin_ids = [42]
        self.controller.user_states = {}
        self.controller.bot = self.bot
        self.controller.manager = NS(_lock=threading.RLock(), bots={1:self.account}, settings=None)
        self.controller._setup_handlers()

    def press(self, uid=42, chat_id=42, typ='private'):
        self.bot.callback(NS(data='export_accounts', id='cb', from_user=NS(id=uid),
                             message=NS(chat=NS(id=chat_id,type=typ), message_id=1)))

    def test_file_contains_account_and_does_not_stop(self):
        self.press()
        self.assertEqual(self.documents[0][:3], (42, 'rakcrmp_accounts.txt', 'Test_User:pass\\:word:ARZAMAS\n'))
        self.account.stop.assert_not_called()
        self.assertEqual(len(self.controller.manager.bots), 1)

    def test_export_requires_admin_and_private_chat(self):
        self.press(uid=10,chat_id=10)
        self.press(chat_id=-100,typ='supergroup')
        self.controller.admin_ids=[]
        self.press()
        self.assertEqual(self.documents, [])

    def test_empty_accounts(self):
        self.controller.manager.bots.clear()
        self.press()
        self.assertEqual(self.documents, [])
        self.assertIn('нет аккаунтов', self.bot.answer.call_args.args[1])

    def test_newlines_cannot_create_extra_rows(self):
        self.account.nickname = 'one\ntwo'
        self.press()
        self.assertEqual(self.documents[0][2].count('\n'),1)
        self.assertIn('one\\ntwo',self.documents[0][2])

if __name__ == '__main__':
    unittest.main()
