"""Lua API v2: live harness, timers and offline playground without a server."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from logic.events import emitter
from logic.lua_engine import LuaScriptError, find_lua
from logic.lua_runtime import LuaRuntime, MAX_TIMERS
from logic import lua_sandbox, lua_storage
from test_lua_integration import Bot, until

@unittest.skipUnless(find_lua(), 'Lua 5.4 unavailable')
class ApiV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.runtimes=[]
        self.storage_patch=mock.patch.object(lua_storage,'ROOT',str(Path(self.tmp.name)/'state'))
        self.storage_patch.start()
    def tearDown(self):
        for rt in self.runtimes:rt.stop()
        self.storage_patch.stop();self.tmp.cleanup()
    def start(self,source,*,armed=False):
        path=Path(self.tmp.name)/'api.lua';path.write_text(source,encoding='utf-8')
        bot=Bot(1);bot.nickname='Test_Player';bot.server_name='ARZAMAS';bot.status='В игре';bot.level_str='3 lvl';bot.target_lvl=5
        rt=LuaRuntime(bot,str(path),armed=armed,tick=.05).start();self.runtimes.append(rt)
        return bot,rt
    def test_helpers_and_account_snapshot(self):
        source=r'''
        function on_chat(e)
          local a=br.account(); assert(a.nickname=='Test_Player' and a.server=='ARZAMAS' and a.target_level==5)
          assert(br.strip_colors('{FF0000}ПрИвЕТ')=='ПрИвЕТ')
          assert(br.contains('ПРИВЕТ мир','привет',true))
          local i,t=br.find_dialog_row('Заголовок\n{FFFFFF}1. СТАТИСТИКА\n2. Опции','статистика',5,true)
          assert(i==0 and br.contains(t,'СТАТИСТИКА'))
          assert(br.distance({x=0,y=0,z=0},{x=3,y=4,z=12})==13)
          assert(br.distance2d({x=0,y=0},{x=3,y=4})==5)
          assert(br.in_radius({x=0,y=0,z=0},{x=3,y=4,z=0},5))
          assert(br.hex_encode('A\0')=='4100' and br.hex_decode('4100')=='A\0')
          assert(br.hex_decode('GG')==nil)
          local x=br.json_decode('{"x":1,"a":[],"v":null}')
          assert(x.x==1 and x.v==br.null and br.json_encode(x.a)=='[]')
          assert(br.json_decode('{"a":1} extra')==nil)
          br.log('helpers-ok')
        end
        '''
        bot,rt=self.start(source);emitter.emit('onShowChat',bot.api,'go')
        self.assertTrue(until(lambda:any('helpers-ok' in x for x in bot.logs)),rt.failure)
    def test_named_repeated_timer_cancel_and_tagged_dry_action(self):
        source='''
        function on_load() br.timer_start('repeat',0.02,'on_count',{value=7},0.05) end
        function on_count(e)
          br.log('tick-'..e.count..'-'..e.data.value)
          if e.count==2 then br.timer_cancel(e.timer_id); br.request({kind='chat',text='/secret'},'chat-1') end
        end
        function on_action_result(e)
          if e.source=='on_count' then for _,v in ipairs(e.results) do if v.tag then br.log('result-'..v.tag..'-'..v.status) end end end
        end
        '''
        bot,rt=self.start(source)
        self.assertTrue(until(lambda:any('result-chat-1-skipped' in s for s in bot.logs)),rt.failure)
        time.sleep(.2);self.assertFalse(any('tick-3-' in s for s in bot.logs));self.assertFalse(rt._timers)
    def test_timer_replace_bounds_and_capacity(self):
        bot,rt=self.start('function on_load() end')
        for i in range(600):rt._named_timer_action({'kind':'timer_start','id':'same','sec':100,'name':'on_timer'})
        self.assertEqual(len(rt._timers),1)
        with self.assertRaises(LuaScriptError):rt._named_timer_action({'kind':'timer_start','id':'x','sec':float('nan'),'name':'on_timer'})
        with self.assertRaises(LuaScriptError):rt._named_timer_action({'kind':'timer_start','id':'x','sec':1,'repeat':0.001,'name':'on_timer'})
        rt._named_timer_action({'kind':'timer_cancel','id':'same'});self.assertFalse(rt._timers)
    def test_named_timer_preserves_false_and_null(self):
        source='''
        function on_load()
          br.timer_start('false',0.02,'on_value',false)
          br.timer_start('null',0.03,'on_value',br.null)
        end
        function on_value(e)
          if e.timer_id=='false' then assert(e.data==false) else assert(e.data==br.null) end
          br.log('value-'..e.timer_id)
        end
        '''
        bot,rt=self.start(source)
        self.assertTrue(until(lambda:any('value-null' in s for s in bot.logs)),rt.failure)
        self.assertTrue(any('value-false' in s for s in bot.logs))
    def test_observed_checkpoint_and_dialog_clear(self):
        source='''
        function on_checkpoint(e) assert(br.checkpoint().x==10); br.log('point-ok') end
        function on_dialog(e) assert(br.last_dialog().title=='Меню'); br.log('dialog-ok') end
        function on_checkpoint_clear(e) assert(br.checkpoint()==nil); br.log('clear-ok') end
        '''
        bot,rt=self.start(source)
        emitter.emit('OnSetCheckpoint',bot.api,10,20,30,2)
        emitter.emit('onShowDialog',bot.api,0,2,'Меню','Статистика','OK','Назад')
        emitter.emit('OnDisableCheckpoint',bot.api)
        self.assertTrue(until(lambda:any('clear-ok' in s for s in bot.logs)),rt.failure)
        self.assertTrue(any('point-ok' in s for s in bot.logs));self.assertTrue(any('dialog-ok' in s for s in bot.logs))
    def test_storage_persists_is_scoped_and_dry_is_read_only(self):
        source="""
        function on_chat(e)
          if e.message=='save' then br.storage_set('step', {n=5})
          elseif e.message=='read' then local v=br.storage_get('step');br.log(v and 'saved-'..v.n or 'empty')
          elseif e.message=='delete' then br.storage_delete('step') end
        end
        """
        bot,rt=self.start(source,armed=True)
        emitter.emit('onShowChat',bot.api,'save')
        self.assertTrue(until(lambda:rt.script.storage.snapshot().get('step',{}).get('n')==5))
        rt.stop();self.runtimes.remove(rt)
        bot2,rt2=self.start(source,armed=False)
        emitter.emit('onShowChat',bot2.api,'read')
        self.assertTrue(until(lambda:any('saved-5' in x for x in bot2.logs)),rt2.failure)
        emitter.emit('onShowChat',bot2.api,'delete');self.assertTrue(until(lambda:rt2.script.stats['skipped']>=1))
        self.assertEqual(rt2.script.storage.snapshot()['step']['n'],5)
        other=Bot(2);other.nickname='Other';other.server_name='ARZAMAS'
        separate=lua_storage.LuaStorage(other,rt2.script.path);self.assertEqual(separate.snapshot(),{})
    def test_storage_limits_and_corrupt_file_never_overwritten(self):
        bot,rt=self.start('function on_load() end',armed=True)
        store=rt.script.storage
        with self.assertRaises(lua_storage.LuaStorageError):store.apply('store_set','key','x'*4001)
        os.makedirs(lua_storage.ROOT,exist_ok=True);Path(store.path).write_text('{broken',encoding='utf8')
        broken=lua_storage.LuaStorage(bot,rt.script.path)
        with self.assertRaises(lua_storage.LuaStorageError):broken.apply('store_set','x',1)
        self.assertEqual(Path(store.path).read_text(),'{broken')
    def test_virtual_storage_in_playground(self):
        source="""
        function on_load() br.storage_set('step',7);br.after(0.1,'on_check',{}) end
        function on_check(e) assert(br.storage_get('step')==7);br.log('virtual-ok') end
        """
        path=Path(self.tmp.name)/'storage.lua';path.write_text(source);lines=[]
        lua_sandbox.run('storage.lua',root=self.tmp.name,output=lines.append)
        self.assertTrue(any('virtual-ok' in line for line in lines));self.assertFalse(os.path.exists(lua_storage.ROOT))
    def test_playground_supports_repeat_cancel_and_dry(self):
        source='''
        function on_load() br.timer_start('pulse',0.1,'on_pulse',{},0.1) end
        function on_pulse(e)
          br.log('pulse '..e.count)
          if e.count==2 then br.timer_cancel('pulse');br.request({kind='chat',text='/x'},'t1') end
        end
        function on_action_result(e) for _,r in ipairs(e.results) do if r.tag then br.log(r.tag..' '..r.status) end end end
        '''
        path=Path(self.tmp.name)/'play.lua';path.write_text(source);lines=[]
        count=lua_sandbox.run('play.lua',root=self.tmp.name,output=lines.append)
        self.assertLess(count,20);self.assertTrue(any('pulse 2' in x for x in lines));self.assertTrue(any('t1 skipped' in x for x in lines));self.assertFalse(any('pulse 3' in x for x in lines))
