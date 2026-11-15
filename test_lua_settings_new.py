import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from logic.lua_settings import load_settings

class SettingsTests(unittest.TestCase):
    def test_config_is_per_script_and_ignores_invalid(self):
        with tempfile.TemporaryDirectory() as tmp, patch('logic.lua_settings.ROOT',Path(tmp)):
            directory=Path(tmp)/'.lua-settings';directory.mkdir()
            (directory/'worker.lua.json').write_text(json.dumps({'timeout':12,'delay':0.5,'маршрут':{'точки':[1,2]}}))
            self.assertEqual(load_settings('/tmp/worker.lua'),{'timeout':12,'delay':0.5,'маршрут':{'точки':[1,2]}})
            (directory/'worker.lua.json').write_text(json.dumps({'enabled':False,'values':{'timeout':99}}))
            self.assertEqual(load_settings('/tmp/worker.lua'),{})
            self.assertEqual(load_settings('/tmp/other.lua'),{})
            self.assertEqual(load_settings('/tmp/../bad.lua/evil.txt'),{})

class LuaSettingsIntegration(unittest.TestCase):
    def test_lua_reads_settings_and_disabled_defaults(self):
        from logic.lua_engine import LuaScript, find_lua
        if not find_lua(): self.skipTest('Lua unavailable')
        with tempfile.TemporaryDirectory() as tmp, patch('logic.lua_settings.ROOT',Path(tmp)):
            root=Path(tmp); d=root/'.lua-settings';d.mkdir(); path=root/'worker.lua'
            path.write_text("function on_load() br.log('start='..br.setting('delay',9)); local r=br.setting('маршрут',{}); br.log('nested='..r['точки'][2]) end\nfunction on_tick() br.log('tick='..br.setting('delay',9)) end")
            config=d/'worker.lua.json';config.write_text(json.dumps({'enabled':True,'values':{'delay':2,'маршрут':{'точки':[1,7]}}}))
            logs=[];script=LuaScript(path,log=logs.append)
            try:
                script.load();self.assertTrue(any('start=2' in x for x in logs));self.assertTrue(any('nested=7' in x for x in logs))
                config.write_text(json.dumps({'enabled':False,'values':{'delay':2}}))
                script.feed('on_tick');self.assertTrue(any('tick=9' in x for x in logs))
            finally: script.close()
