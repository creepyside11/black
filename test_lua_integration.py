import os
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from collections import deque
from unittest.mock import patch
from logic.events import emitter
from logic.lua_runtime import LuaRuntime, resolve_script
from logic.lua_engine import find_lua

class Session:
    connected = True
    def __init__(self): self.messages=[];self.named_messages=[]
    def send_json(self, iid, data, **kw):
        self.messages.append((iid,data));self.named_messages.append((iid,data,kw));return True
class API:
    def __init__(self): self.session=Session();self.rpcs=[]
    def getbotposition(self):return (1.,2.,3.)
    def getbotvehicle(self):return 0
    def SendRPC(self, rid, data):self.rpcs.append((rid,data))
class Bot:
    def __init__(self, n):self.bot_id=n;self.api=API();self.session=self.api.session;self.logs=[];self.lua_spawn_control=False
    def add_log(self,text):self.logs.append(text)

def until(predicate, timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return True
        time.sleep(.01)
    return False

@unittest.skipUnless(find_lua(), 'Lua interpreter unavailable')
class Integration(unittest.TestCase):
    def setUp(self): self.dir=tempfile.TemporaryDirectory();self.runtimes=[]
    def tearDown(self):
        for x in self.runtimes:x.stop()
        self.dir.cleanup()
    def script(self, content):
        p=os.path.join(self.dir.name,'test.lua')
        with open(p,'w') as f:f.write(content)
        return p
    def start(self,bot,src,**kw):
        r=LuaRuntime(bot,self.script(src),tick=.05,**kw).start();self.runtimes.append(r);return r
    def test_own_account_events_timers_and_actions(self):
        bot,other=Bot(1),Bot(2)
        r=self.start(bot,'''function on_json(e)
          if e.interface_id == 31 then br.after(0.02, 'on_answer', {id=e.data.id}) end
        end
        function on_answer(e) br.select_textdraw(e.id); br.send_json(31,{s=1}) end''')
        emitter.emit('onReceiveJSON',other.api,31,{'id':99})
        emitter.emit('onReceiveJSON',bot.api,31,{'id':2052})
        self.assertTrue(until(lambda:len(bot.api.rpcs)==1 and len(bot.session.messages)==1))
        self.assertEqual(bot.api.rpcs,[(437,b'\x04\x08')])
        self.assertEqual(bot.session.messages,[(31,{'s':1})])
        self.assertEqual(other.api.rpcs,[])
    def test_dry_mode_skips_mutations(self):
        bot=Bot(1);r=self.start(bot,'function on_chat(e) br.send_json(31,{s=1}) end',armed=False)
        emitter.emit('onShowChat',bot.api,'test')
        self.assertTrue(until(lambda:r.script.stats['skipped']==1))
        self.assertEqual(bot.session.messages,[])
    def test_custom_event_registration(self):
        bot=Bot(1);r=self.start(bot,'''br.event('on_custom', function(e) br.log('value=' .. e.value) end)
          function on_load(e) br.after(0.01, 'on_custom', {value=7}) end''')
        self.assertTrue(until(lambda:any('value=7' in x for x in bot.logs)))
    def test_print_and_br_log_reach_account_log_and_separate_history(self):
        from tg_controller import _format_account_logs
        bot=Bot(91); bot.logs=deque(maxlen=3); bot.lua_logs=deque(maxlen=200)
        self.start(bot,'''print('при загрузке <test> & ok')
            function on_load(e) br.log('password=secret') end
            function on_chat(e) print('из события', e.message) end''')
        emitter.emit('onShowChat',bot.api,'hello')
        self.assertTrue(until(lambda:any('из события' in x for x in bot.lua_logs)))
        text=_format_account_logs(bot, lua_only=True)
        self.assertIn('при загрузке &lt;test&gt; &amp; ok',text)
        self.assertIn('password=[СКРЫТО]',text)
        self.assertNotIn('secret',text)
        for i in range(20): bot.add_log('шум чата')
        self.assertIn('при загрузке',_format_account_logs(bot, lua_only=True))
        self.assertFalse(any('при загрузке' in line for line in bot.logs))
    def test_callback_errors_stop_with_trace(self):
        bot=Bot(1);r=self.start(bot,'''function on_chat(e) error('bad script') end''')
        emitter.emit('onShowChat',bot.api,'x')
        self.assertTrue(until(lambda:bool(r.failure)))
        self.assertIn('bad script',r.failure)
    def test_infinite_handler_is_terminated(self):
        bot=Bot(1)
        runtime=self.start(bot,"function on_chat(e) while true do end end")
        runtime.script.engine.call_timeout = 0.2
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:bool(runtime.failure), timeout=2))
        self.assertFalse(runtime.script.engine.alive)

    def test_rejected_action_is_reported(self):
        bot=Bot(1);bot.session.connected=False
        runtime=self.start(bot,"function on_chat(e) br.send_json(31,{s=1}) end")
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:runtime.script.stats['errors'] == 1))
        self.assertEqual(bot.session.messages,[])

    def test_script_path_escape_rejected(self):
        with self.assertRaises(ValueError):resolve_script('../../etc/passwd.lua')
    def test_standalone_enabled_before_connect_and_selects_spawn(self):
        import logic.lua_runtime as module
        bot=Bot(4);bot.session.connected=False;bot.active_task=None;bot.lua_runtime=None;bot.lua_ui_control=False
        def start():
            self.assertTrue(bot.lua_spawn_control)
            self.assertTrue(bot.lua_ui_control)
            bot.session.connected=True
            return True
        bot.start=start
        class Manager: bots={4:bot}
        p=self.script("function on_spawn_menu(e) br.spawn_select(e.data.m[1]) end")
        with patch.object(module,'SCRIPT_DIR',self.dir.name), patch('logic.spawn.select_spawn',return_value=True) as choose:
            module.handle_command(Manager(), '/lua bot 4 test.lua')
            r=bot.lua_runtime;self.runtimes.append(r)
            self.assertTrue(r.running)
            emitter.emit('onSpawnSelect',bot.api,{'m':[13,14]})
            self.assertTrue(until(lambda:choose.called))
            self.assertEqual(choose.call_args.args[:2],(bot,13))
            r.stop()
            self.assertFalse(bot.lua_spawn_control)
            self.assertFalse(bot.lua_ui_control)


@unittest.skipUnless(find_lua(), 'Lua interpreter unavailable')
class ReviewRegressions(Integration):
    def test_stop_during_sleep_does_not_execute_following_action(self):
        bot=Bot(1)
        r=self.start(bot,'''function on_chat(e)
          br.log('sleep-start'); br.sleep(2); br.send_json(31,{s=1})
        end''')
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:any('sleep-start' in x for x in bot.logs)))
        r.stop()
        self.assertEqual(bot.session.messages,[])

    def test_json_arrays_and_null_round_trip(self):
        bot=Bot(1)
        r=self.start(bot,'''function on_json(e)
           if e.interface_id==41 then br.send_json(42,e.data) end
        end''')
        data={'empty':[], 'nested':[1,None,3], 'object':{}, 'nothing':None}
        emitter.emit('onReceiveJSON',bot.api,41,data)
        self.assertTrue(until(lambda:bool(bot.session.messages)))
        self.assertEqual(bot.session.messages[-1],(42,data))

    def test_disconnected_snapshot_is_nil(self):
        bot=Bot(1);bot.api=None;bot.session=None
        r=self.start(bot,'''function on_tick(e)
          assert(br.pos() == nil); assert(br.health() == nil); assert(br.vehicle_id() == nil)
          br.log('nil-snapshot-ok')
        end''')
        self.assertTrue(until(lambda:any('nil-snapshot-ok' in x for x in bot.logs)))
        self.assertFalse(r.failure)

    def test_stopping_releases_event_listeners(self):
        bot=Bot(1)
        before=len(emitter._sync_listeners)
        r=self.start(bot,'function on_tick(e) end')
        self.assertGreater(len(emitter._sync_listeners),before)
        r.stop()
        self.assertEqual(len(emitter._sync_listeners),before)

    def test_explicit_empty_array_and_null_in_outgoing_json(self):
        bot=Bot(1)
        r=self.start(bot,'''function on_chat(e)
          br.send_json(45, {list=br.array(), empty={}, missing=br.null})
        end''')
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:bool(bot.session.messages)))
        self.assertEqual(bot.session.messages[-1],(45,{'list':[], 'empty':{}, 'missing':None}))

    def test_captcha_response_is_discarded_after_script_stop(self):
        bot=Bot(1);started=threading.Event();release=threading.Event();finished=threading.Event()
        class SlowSolver:
            def __init__(self, **kw): pass
            def handle(self, session, challenge):
                started.set();release.wait(2)
                session.send_json(26,{'s':'test','t':0});finished.set()
        src='''function on_json(e) if e.interface_id == 26 and e.data.o == 1 then br.solve_captcha(e.data) end end'''
        with patch('logic.captcha.CaptchaAnswerer',SlowSolver):
            r=self.start(bot,src)
            emitter.emit('onReceiveJSON',bot.api,26,{'o':1,'u':'https://example.invalid/image'})
            self.assertTrue(started.wait(1))
            r.stop();release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(bot.session.messages,[])

    def test_captcha_closed_before_solver_result_is_discarded(self):
        bot=Bot(1);started=threading.Event();release=threading.Event();finished=threading.Event()
        class SlowSolver:
            def __init__(self, **kw): pass
            def handle(self, session, challenge):
                started.set();release.wait(2)
                session.send_json(26,{'s':'test','t':0});finished.set()
        src='''function on_json(e) if e.interface_id == 26 and e.data.o == 1 then br.solve_captcha(e.data) end end'''
        with patch('logic.captcha.CaptchaAnswerer',SlowSolver):
            r=self.start(bot,src)
            emitter.emit('onReceiveJSON',bot.api,26,{'o':1,'u':'https://example.invalid/image'})
            self.assertTrue(started.wait(1))
            emitter.emit('onReceiveJSON',bot.api,26,{'c':1})
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(bot.session.messages,[])



    def test_captcha_solver_can_send_current_result(self):
        bot=Bot(1);finished=threading.Event()
        class Solver:
            def __init__(self, **kw): pass
            def handle(self, session, challenge):
                session.send_json(26,{'s':'test','t':0});finished.set()
        src='function on_json(e) if e.interface_id==26 and e.data.o==1 then br.solve_captcha(e.data) end end'
        with patch('logic.captcha.CaptchaAnswerer',Solver):
            r=self.start(bot,src)
            emitter.emit('onReceiveJSON',bot.api,26,{'o':1,'u':'https://example.invalid/image'})
            self.assertTrue(finished.wait(1))
        self.assertEqual(bot.session.messages,[(26,{'s':'test','t':0})])

    def test_invalid_action_is_reported_without_killing_runtime(self):
        bot=Bot(1)
        r=self.start(bot,"function on_chat(e) br.emit({kind='walk',x='wrong',y=1,z=2}) end")
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:r.script.stats['errors']>0))
        self.assertTrue(r.running)
        self.assertEqual(r.failure,'')

    def test_unknown_dry_option_cannot_start_script(self):
        import logic.lua_runtime as module
        import contextlib,io
        bot=Bot(1);bot.lua_runtime=None
        class Manager: bots={1:bot}
        self.script('function on_tick(e) end')
        out=io.StringIO()
        with patch.object(module,'SCRIPT_DIR',self.dir.name),contextlib.redirect_stdout(out):
            module.handle_command(Manager(),'/lua addon 1 test.lua --dru')
        self.assertIsNone(bot.lua_runtime)
        self.assertIn('неизвестные параметры',out.getvalue())



    def test_stale_captcha_event_is_not_started_after_close(self):
        bot=Bot(1);started=threading.Event()
        class Solver:
            def __init__(self, **kw): started.set()
            def handle(self, session, challenge): raise AssertionError('stale challenge')
        r=self.start(bot,'function on_tick(e) end')
        r._observe_captcha({'o':1,'u':'https://example.invalid/a'})
        r._observe_captcha({'c':1})
        with patch('logic.captcha.CaptchaAnswerer',Solver):
            self.assertFalse(r.request_captcha({'o':1,'u':'https://example.invalid/a'}))
        self.assertFalse(started.is_set())

    def test_listener_sync_flag_stays_until_last_subscription(self):
        from logic.events import EventEmitter
        bus=EventEmitter()
        def callback(*args):pass
        bus.subscribe('one',callback,sync=True)
        bus.subscribe('two',callback,key=7,sync=True)
        bus.off('one',callback)
        self.assertTrue(bus._is_sync_listener(callback))
        bus.off('two',callback,key=7)
        self.assertFalse(bus._is_sync_listener(callback))
        bus.shutdown(wait=True)



    def test_spawn_menu_uses_real_spawn_encoder(self):
        import logic.spawn
        bot=Bot(1)
        r=self.start(bot,"function on_spawn_menu(e) br.spawn_select(e.data.m[1]) end",mode='bot')
        logic.spawn.remember_spawn_menu({'m':[17,19]},bot)
        emitter.emit('onSpawnSelect',bot.api,{'m':[17,19]})
        self.assertTrue(until(lambda:bool(bot.session.messages)))
        self.assertEqual(bot.session.messages,[(50,{'t':1,'s':17})])

    def test_mine_example_does_not_answer_closed_minigame(self):
        bot=Bot(1)
        path=os.path.join(os.path.dirname(__file__),'scripts','lua','minigame_example.lua')
        r=LuaRuntime(bot,path,mode='bot',tick=.05).start();self.runtimes.append(r)
        emitter.emit('onReceiveJSON',bot.api,31,{'o':1,'g':1,'p_start':0,'p_max':50,'p_title':'Раскопка руды'})
        self.assertTrue(until(lambda:r.report()['pending_timers']==1))
        emitter.emit('onReceiveJSON',bot.api,31,{'c':1})
        self.assertTrue(until(lambda:r.events.empty()))
        # Exercise the scheduled callback immediately without waiting 6.83 s.
        entry=r._timers[0]
        r._timers[0]=(time.monotonic()-1,*entry[1:])
        self.assertTrue(until(lambda:not r._timers))
        self.assertEqual(bot.session.messages,[])



    def test_actual_bot_hooks_and_real_spawn_payload_offline(self):
        source = self.script("function on_spawn_menu(e) br.spawn_select(e.data.m[1]) end")
        code = '''
import sys, threading, time
from bot import BotInstance, BotManager
from logic.events import emitter
from logic.lua_runtime import LuaRuntime
class Session:
    connected=True
    def __init__(self): self.messages=[]
    def send_json(self,iid,data,**kw): self.messages.append((iid,data)); return True
class API:
    def __init__(self): self.session=Session()
    def getbotposition(self): return (0,0,0)
    def getbotvehicle(self): return 0
m=BotManager.__new__(BotManager)
m._obj_index={};m.bots={};m._lock=threading.RLock()
b=BotInstance(1,'127.0.0.1',5125,'Local_Test','dummy',manager=m,default_task='stop')
b.api=API();b.session=b.api.session;m.bots[1]=b;m._obj_index[id(b.api)]=1
m._setup_event_hooks()
b.lua_spawn_control=True;b.lua_ui_control=True
r=LuaRuntime(b,sys.argv[1],mode='bot',tick=.05);b.lua_runtime=r;r.start()
emitter.emit('onSpawnSelect',b.api,{'o':1,'m':[17,19]})
deadline=time.monotonic()+3
while not b.session.messages and time.monotonic()<deadline: time.sleep(.01)
time.sleep(.2)
assert b.session.messages==[(50,{'t':1,'s':17})],repr(b.session.messages)
r.stop();emitter.shutdown(wait=True)
'''
        result=subprocess.run([sys.executable,'-c',code,source],capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)



    def test_invalid_script_does_not_start_account(self):
        import logic.lua_runtime as module
        import contextlib,io
        bot=Bot(1);bot.session.connected=False;bot.active_task=None;bot.lua_runtime=None
        bot.lua_spawn_control=False;bot.lua_ui_control=False;called=[]
        bot.start=lambda:called.append(True)
        class Manager: bots={1:bot}
        self.script('function on_tick( this is invalid')
        out=io.StringIO()
        with patch.object(module,'SCRIPT_DIR',self.dir.name),contextlib.redirect_stdout(out):
            module.handle_command(Manager(),'/lua bot 1 test.lua')
        self.assertIsNone(bot.lua_runtime)
        self.assertFalse(bot.lua_spawn_control)
        self.assertFalse(bot.lua_ui_control)
        self.assertFalse(called)
        self.assertIn('скрипт не загрузился',out.getvalue())

    def test_optional_raw_packets_are_scoped_to_account(self):
        bot,other=Bot(1),Bot(2)
        src="""function on_packet(e) br.send_json(44,{id=e.packet_id,body=e.body_hex}) end"""
        r=self.start(bot,src,packets=True)
        emitter.emit('onReceivePacket',other.api,7,b'wrong')
        emitter.emit('onReceivePacket',bot.api,8,bytes.fromhex('1020'))
        self.assertTrue(until(lambda:bool(bot.session.messages)))
        self.assertEqual(bot.session.messages,[(44,{'id':8,'body':'1020'})])



    def test_long_on_load_sleep_does_not_block_start(self):
        bot=Bot(1)
        before=time.monotonic()
        r=self.start(bot,"function on_load(e) br.sleep(2); br.send_json(31,{s=1}) end")
        self.assertLess(time.monotonic()-before,1)
        r.stop()
        self.assertEqual(bot.session.messages,[])



    def test_npc_uses_expected_interface_delivery_profile(self):
        bot=Bot(1)
        r=self.start(bot,"function on_chat(e) br.npc(2) end")
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:bool(bot.session.named_messages)))
        self.assertEqual(bot.session.named_messages[-1],(63,{'bk':2},{'packet_name':'interface_npc_dialog_response'}))

    def test_invalid_delivery_profile_is_rejected(self):
        bot=Bot(1)
        r=self.start(bot,"function on_chat(e) br.send_json(31,{s=1},'bad_profile') end")
        emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:r.script.stats['errors']>0))
        self.assertEqual(bot.session.messages,[])
        self.assertTrue(r.running)

if __name__=='__main__':unittest.main()
