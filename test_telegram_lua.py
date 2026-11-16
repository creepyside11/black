"""Telegram Lua upload, launch and wizard tests without Telegram or game network."""
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
import bot as botmod
from logic.events import emitter
from logic.lua_engine import find_lua
import tg_controller as tg
from logic import lua_runtime, lua_telegram

class UploadTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.p=mock.patch.object(lua_runtime,'SCRIPT_DIR',self.tmp.name);self.p.start()
 def tearDown(self):self.p.stop();self.tmp.cleanup()
 def test_valid_upload_and_no_overwrite(self):
  self.assertEqual(lua_telegram.store_script('worker.lua',b'function on_load() end'),'worker.lua')
  with self.assertRaises(ValueError):lua_telegram.store_script('worker.lua',b'other')
  self.assertEqual((Path(self.tmp.name)/'worker.lua').read_text(),'function on_load() end')
 def test_rejects_escapes_binary_and_oversized(self):
  for name,content in [('../a.lua',b'hi'),('a..lua',b'hi'),('a.luac',b'hi'),('a.LUA',b'hi'),('a.lua',b'\0'),('a.lua',b'\xff'),('a.lua',b'a'*(lua_telegram.MAX_SCRIPT_BYTES+1))]:
   with self.subTest(name=name):
    with self.assertRaises(ValueError):lua_telegram.store_script(name,content)

class StartupTests(unittest.TestCase):
 def fake(self):
  b=botmod.BotInstance.__new__(botmod.BotInstance);b.is_banned=False;b.status='';b.default_task_name='lua:work.lua'
  b.lua_runtime=None;b.lua_spawn_control=False;b.lua_ui_control=False;b.active_task=None;b.session=None;b.thread=None;b.manager=None
  b.check_proxy_before_start=mock.Mock(return_value=(True,''));b._hold_proxy_bridge=mock.Mock();b._release_proxy_bridge=mock.Mock();b.cancel_reconnect=mock.Mock();b.add_log=mock.Mock();b._run=mock.Mock()
  return b
 def test_lua_is_prepared_before_session_setup(self):
  b=self.fake();order=[]
  def prepare(bot,name,mode):order.append(('lua',name,mode));bot.lua_spawn_control=True;return NS(stop=mock.Mock())
  def setup():order.append(('session',b.lua_spawn_control));return True
  b.setup_session=setup
  with mock.patch.object(lua_telegram,'prepare_runtime',side_effect=prepare),mock.patch.object(botmod.threading,'Thread') as thread:
   self.assertTrue(b.start());thread.return_value.start.assert_called_once()
  self.assertEqual(order,[('lua','work.lua','bot'),('session',True)])
 @unittest.skipUnless(find_lua(), 'Lua 5.4 is unavailable')
 def test_real_lua_bot_starts_before_connection_and_handles_spawn(self):
  b=self.fake();b.bot_id=10;b.nickname='Local_Test';b.api=None
  class Session:
   connected=True
   def __init__(self):self.messages=[]
   def send_json(self,iid,data,**kw):self.messages.append((iid,data));return True
  with tempfile.TemporaryDirectory() as directory:
   (Path(directory)/'work.lua').write_text('function on_spawn_menu(e) br.spawn_select(e.data.m[1]) end')
   def setup():
    self.assertTrue(b.lua_spawn_control);self.assertTrue(b.lua_ui_control)
    b.session=Session();b.api=NS(session=b.session,getbotposition=lambda:(0,0,0),getbotvehicle=lambda:0)
    return True
   b.setup_session=setup
   with mock.patch.object(lua_runtime,'SCRIPT_DIR',directory):
    self.assertTrue(b.start());self.assertTrue(b.lua_runtime.running)
    with mock.patch('logic.spawn.select_spawn',return_value=True) as choose:
     emitter.emit('onSpawnSelect',b.api,{'m':[17,19]})
     end=time.monotonic()+2
     while not choose.called and time.monotonic()<end:time.sleep(.01)
     self.assertTrue(choose.called, f'failure={b.lua_runtime.failure}, report={b.lua_runtime.script.report()}, logs={b.add_log.call_args_list}, queued={b.lua_runtime.events.qsize()}');self.assertEqual(choose.call_args.args[:2],(b,17))
    b.lua_runtime.stop()
 def test_failed_lua_prevents_connection(self):
  b=self.fake();b.setup_session=mock.Mock()
  with mock.patch.object(lua_telegram,'prepare_runtime',side_effect=ValueError('missing')):
   self.assertFalse(b.start())
  b.setup_session.assert_not_called();self.assertEqual(b.status,'Ошибка Lua-скрипта')
 def test_session_failure_stops_prepared_runtime(self):
  b=self.fake();b.setup_session=mock.Mock(return_value=False);runtime=NS(stop=mock.Mock())
  with mock.patch.object(lua_telegram,'prepare_runtime',return_value=runtime):self.assertFalse(b.start())
  runtime.stop.assert_called_once();b._release_proxy_bridge.assert_called_once()

class FakeTelegram:
 def __init__(self):
  self.handlers={};self.callback=None;self.sent=[];self.replies=[];self.answer=mock.Mock()
 def message_handler(self,**kw):
  def deco(fn):self.handlers[fn.__name__]=fn;return fn
  return deco
 def callback_query_handler(self,**kw):
  def deco(fn):self.callback=fn;return fn
  return deco
 def send_message(self,chat,text,reply_markup=None,**kw):self.sent.append((chat,text,reply_markup));return NS(message_id=1)
 def reply_to(self,msg,text):self.replies.append(text)
 def answer_callback_query(self,*a,**kw):self.answer(*a,**kw)
 def get_file(self,id):return NS(file_size=10,file_path='safe')
 def download_file(self,path):return b'function on_load() end'

class FlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.patch=mock.patch.object(lua_runtime,'SCRIPT_DIR',self.tmp.name);self.patch.start()
  lua_telegram.store_script('worker.lua',b'function on_load() end')
  self.c=tg.TelegramBotController.__new__(tg.TelegramBotController);self.c.admin_ids=[42];self.c.user_states={};self.c.bot=FakeTelegram()
  self.c.manager=NS(_lock=threading.RLock(),bots={},settings=None);self.c._setup_handlers()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def call(self,data,uid=42,typ='private'):
  self.c.bot.callback(NS(data=data,id='cb',from_user=NS(id=uid),message=NS(chat=NS(id=uid,type=typ),message_id=1)))
 def test_wizard_lists_lua_and_selects_bot_before_proxy_step(self):
  self.c._ask_batch_task(42,'TVER',2,'example')
  self.assertTrue(any(b.callback_data=='batch_lua' for row in self.c.bot.sent[-1][2].keyboard for b in row))
  self.call('batch_lua');self.call('batch_lua_pick:0')
  state=self.c.user_states[42];self.assertEqual(state['action'],'batch_proxy');self.assertEqual(state['task'],'lua:worker.lua')
 def test_upload_requires_admin_private_chat(self):
  self.call('lua_upload',uid=42,typ='group');self.assertNotIn(42,self.c.user_states)
  self.call('lua_upload',uid=7);self.assertNotIn(7,self.c.user_states)
  self.call('lua_upload');self.assertEqual(self.c.user_states[42]['action'],'lua_upload')
  msg=NS(from_user=NS(id=42),chat=NS(id=42,type='private'),document=NS(file_id='x',file_size=10,file_name='new.lua'))
  self.c.bot.handlers['handle_lua_document'](msg)
  self.assertTrue((Path(self.tmp.name)/'new.lua').exists());self.assertNotIn(42,self.c.user_states)
 def test_menu_shows_account_start_and_stop(self):
  self.call('lua_account:4:1');buttons=[b.callback_data for row in self.c.bot.sent[-1][2].keyboard for b in row]
  self.assertIn('lua_choose:4:1',buttons);self.assertIn('lua_stop:4:1',buttons)
 def test_account_logs_toggle_escapes_text_and_survives_chat_noise(self):
  b=NS(bot_id=4,nickname='Nick',logs=['обычная строка']*40,lua_logs=['[lua test.lua] a < b & c'])
  self.c.manager.bots[4]=b;self.c.bot.edit_message_text=mock.Mock()
  self.call('bot_logs:4:1')
  args,kw=self.c.bot.edit_message_text.call_args
  buttons=[x.callback_data for row in kw['reply_markup'].keyboard for x in row]
  self.assertIn('bot_lua_logs:4:1',buttons)
  self.call('bot_lua_logs:4:1')
  args,kw=self.c.bot.edit_message_text.call_args
  self.assertIn('a &lt; b &amp; c',args[0]);self.assertNotIn('обычная строка',args[0])
  self.assertIn('bot_logs:4:1',[x.callback_data for row in kw['reply_markup'].keyboard for x in row])
 def test_long_log_is_bounded(self):
  b=NS(logs=['<&'*5000],lua_logs=[])
  self.assertLess(len(tg._format_account_logs(b)),3500)

class ExistingAccountFlowTests(FlowTests):
 def test_both_modes_pass_selected_file_and_account(self):
  for mode in ('addon','bot'):
   with self.subTest(mode=mode),mock.patch.object(self.c,'_lua_start') as start:
    self.call('lua_choose:4:1');self.call('lua_pick:0');self.call('lua_run:'+mode)
    start.assert_called_once_with(4,'worker.lua',mode)
 def test_stopping_lua_keeps_account_connected_and_clears_autostart(self):
  runtime=NS(stop=mock.Mock());b=NS(lua_runtime=runtime,default_task_name='lua:worker.lua')
  self.c.manager.bots[4]=b;self.c.manager.save_accounts=mock.Mock()
  self.call('lua_stop:4:1');runtime.stop.assert_called_once();self.assertIsNone(b.lua_runtime)
  self.assertEqual(b.default_task_name,'stop');self.c.manager.save_accounts.assert_called_once_with(force=True)
