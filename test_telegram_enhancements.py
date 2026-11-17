import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import bot as botmod
import tg_controller as tg
from logic import lua_runtime, lua_telegram, lua_log, system_resources
from test_telegram_lua import FakeTelegram
from test_reconnect import make_bot

class Enhancements(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.p1=mock.patch.object(lua_runtime,'SCRIPT_DIR',str(self.root));self.p1.start()
        self.p2=mock.patch.object(lua_log,'LOG_DIR',str(self.root/'logs'));self.p2.start()
        self.c=tg.TelegramBotController.__new__(tg.TelegramBotController)
        self.c.admin_ids=[42];self.c.user_states={};self.c.bot=FakeTelegram()
        self.c.manager=NS(_lock=threading.Lock(),bots={},settings=None,save_accounts=mock.Mock())
        self.c._setup_handlers()
    def tearDown(self):self.p2.stop();self.p1.stop();self.tmp.cleanup()
    def call(self,data,uid=42,typ='private'):
        self.c.bot.callback(NS(data=data,id='cb',from_user=NS(id=uid),message=NS(chat=NS(id=uid,type=typ),message_id=1)))
    def test_select_and_manage_paginate_past_100(self):
        for i in range(105):(self.root/f'test{i:03d}.lua').write_text('-- lua')
        self.call('lua_list');buttons=[b.callback_data for row in self.c.bot.sent[-1][2].keyboard for b in row]
        self.assertIn('lua_files_page:manage:2',buttons)
        self.call('lua_files_page:manage:14')
        buttons=[b.callback_data for row in self.c.bot.sent[-1][2].keyboard for b in row]
        self.assertIn('lua_delete_pick:104',buttons)
        self.call('lua_choose:7:1');self.call('lua_files_page:account:14')
        buttons=[b.callback_data for row in self.c.bot.sent[-1][2].keyboard for b in row]
        self.assertIn('lua_pick:104',buttons)
    def test_delete_requires_confirmation_and_rejects_running_or_changed_file(self):
        p=self.root/'worker.lua';p.write_text('old')
        self.call('lua_list');self.call('lua_delete_pick:0');self.assertTrue(p.exists())
        p.write_text('changed');self.call('lua_delete_confirm');self.assertTrue(p.exists())
        self.call('lua_list');self.c.manager.bots[1]=NS(default_task_name='lua:worker.lua',lua_runtime=None)
        self.call('lua_delete_pick:0');self.assertNotIn('delete_name',self.c.user_states[42])
        self.c.manager.bots.clear();self.call('lua_delete_pick:0');self.call('lua_delete_confirm');self.assertFalse(p.exists())
    def test_script_selection_rejects_stale_list(self):
        (self.root/'b.lua').write_text('-- b')
        self.call('lua_choose:7:1')
        (self.root/'a.lua').write_text('-- a')
        self.call('lua_pick:0')
        self.assertNotIn('selected',self.c.user_states[42])
        self.assertIn('устарел',self.c.bot.answer.call_args.args[1])
    def test_delete_rejects_symlink_and_nonadmin(self):
        target=self.root/'secret';target.write_text('safe');(self.root/'fake.lua').symlink_to(target)
        with self.assertRaises(ValueError):lua_telegram.deletion_snapshot('fake.lua',self.c.manager)
        self.call('lua_list',uid=5);self.assertNotIn(5,self.c.user_states)
    def test_account_settings_are_private_and_cancel_timer(self):
        b=make_bot();b.bot_id=1;b.nickname='Test';b.stats_interval_override=0;b.stats_enabled=True;b.auto_reconnect=True
        self.c.manager.bots[1]=b
        self.call('acfg:1:1:reconnect',uid=5);self.assertTrue(b.auto_reconnect)
        self.call('acfg:1:1:reconnect');self.assertFalse(b.auto_reconnect);self.assertTrue(b.reconnect_cancel_event.is_set())
        b.schedule_reconnect(.1);self.assertIsNone(b.reconnect_thread)
        self.call('acfg:1:1:interval:900');self.assertEqual(b.stats_interval(),900)
        self.call('acfg:1:1:stats');self.assertFalse(b.stats_enabled)
        self.call('acfg:1:1:delay:60');self.assertEqual(b.reconnect_min_delay,60)
        self.assertEqual(self.c.manager.save_accounts.call_count,4)
    def test_lua_log_download_errors_and_text_search(self):
        logdir=self.root/'logs';logdir.mkdir();(logdir/'account_1.log').write_text('hello\nERROR failed\n')
        self.c.manager.bots[1]=NS(nickname='Name_Test',lua_logs=[])
        docs=[];self.c.bot.send_document=lambda cid,doc,**kw:docs.append((doc.name,doc.read()))
        self.call('lua_log_file:1:1');self.assertIn(b'ERROR failed',docs[0][1])
        self.call('lua_log_errors:1:1');self.assertIn('ERROR failed',self.c.bot.sent[-1][1])
        self.call('lua_log_search:1:1');msg=NS(from_user=NS(id=42),chat=NS(id=42,type='private'),text='hello')
        self.c.bot.handlers['handle_user_text'](msg);self.assertIn('hello',self.c.bot.sent[-1][1])
        self.call('lua_nick_search');msg.text='name';self.c.bot.handlers['handle_user_text'](msg)
        self.assertEqual(self.c.bot.sent[-1][2].keyboard[0][0].callback_data,'bot_lua_logs:1:1')
    def test_read_log_does_not_follow_symlink(self):
        logdir=self.root/'logs';logdir.mkdir();secret=self.root/'secret';secret.write_text('private')
        (logdir/'account_3.log').symlink_to(secret)
        self.assertEqual(lua_log.read_log(3),'')
    def test_resource_button_does_not_stop_account(self):
        account=NS(stop=mock.Mock());self.c.manager.bots[1]=account
        class Immediate:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        with mock.patch('tg_controller.threading.Thread',Immediate):self.call('server_resources')
        self.assertIn('CPU:',self.c.bot.sent[-1][1]);account.stop.assert_not_called()
    def test_disabled_stats_never_sends_mm(self):
        b=make_bot();b.stats_enabled=False;b.session=NS(connected=True);b.status='В игре'
        b.send_chat=mock.Mock();b._stats_tick(10**12);b.send_chat.assert_not_called()
    def test_reconnect_setting_respects_larger_minimum(self):
        b=make_bot();b.auto_reconnect=True;b.reconnect_min_delay=60
        b.schedule_reconnect(delay=1);self.assertIn('60 сек',b.logs[-1])
        b.cancel_reconnect();b.reconnect_thread.join(timeout=2)
    def test_readonly_resource_snapshot(self):
        info=system_resources.snapshot(0.01);out=system_resources.format_snapshot(info)
        self.assertIn('CPU:',out);self.assertIn('RAM:',out);self.assertIn('Диск /:',out)
        self.assertGreater(info['disk_total'],0)

class Persistence(unittest.TestCase):
    def test_saved_settings_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'accounts.json';path.write_text(json.dumps([{'nickname':'Test_User','password':'test','server':'TOLYATTI','ip':'192.0.2.1','port':5125,'stats_enabled':False,'stats_interval_override':900,'auto_reconnect':False,'reconnect_min_delay':60}]))
            with mock.patch.dict(os.environ,{'BR_ACCOUNTS_FILE':str(path)}), mock.patch('br_servers.find_server',return_value=None):
                manager=botmod.BotManager();b=next(iter(manager.bots.values()))
                self.assertFalse(b.stats_enabled);self.assertEqual(b.stats_interval(),900);self.assertFalse(b.auto_reconnect);self.assertEqual(b.reconnect_min_delay,60)
                manager.save_accounts(force=True);saved=json.loads(path.read_text())[0]
                self.assertFalse(saved['stats_enabled']);self.assertEqual(saved['stats_interval_override'],900);self.assertFalse(saved['auto_reconnect']);self.assertEqual(saved['reconnect_min_delay'],60)
