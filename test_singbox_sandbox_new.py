import contextlib, io, json, os, tempfile, threading, time, unittest
from unittest.mock import patch
from logic.vpn_bridge import VpnBridge
from logic.singbox_service import SingBoxService
from logic.lua_sandbox import run, resolve

FAKE='''#!/usr/bin/env python3
import json, socket, sys, time
if sys.argv[1] == "check": sys.exit(0)
with open(sys.argv[3]) as f: cfg=json.load(f)
port=cfg["inbounds"][0]["listen_port"]
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(("127.0.0.1",port));s.listen(8)
while True:
 c,_=s.accept();c.close()
'''
LINK='vless://11111111-2222-3333-4444-555555555555@127.0.0.1:12345?security=none'
class Tests(unittest.TestCase):
 def setUp(self):
  self.d=tempfile.TemporaryDirectory();self.bin=os.path.join(self.d.name,'sing-box')
  with open(self.bin,'w') as f:f.write(FAKE)
  os.chmod(self.bin,0o755)
 def tearDown(self): self.d.cleanup()
 def test_same_bridge_concurrent_reuses_process_and_port(self):
  bridge=VpnBridge(binary=self.bin,state_dir=os.path.join(self.d.name,'state'))
  results=[]
  try:
   threads=[threading.Thread(target=lambda:results.append(bridge.start('node1',LINK))) for _ in range(5)]
   for t in threads:t.start()
   for t in threads:t.join(5)
   self.assertEqual(len(results),5);self.assertTrue(all(results));self.assertEqual(len(set(results)),1)
   self.assertIn('node1',bridge._ports);self.assertIn('node1',bridge._procs)
  finally:bridge.stop_all()
 def test_autostart_service_config_and_stop(self):
  cfg=os.path.join(self.d.name,'config.json')
  with open(cfg,'w') as f:json.dump({'inbounds':[{'listen_port':0}]},f)
  service=SingBoxService(config=cfg,log=lambda m:None)
  with patch('logic.singbox_service.find_binary',return_value=self.bin):
   try:
    self.assertTrue(service.start());self.assertTrue(service.status()['running']);self.assertTrue(service.start())
   finally:service.stop()
   self.assertFalse(service.status()['running'])
 def test_missing_configuration_does_not_start(self):
  service=SingBoxService(config=os.path.join(self.d.name,'missing.json'),log=lambda m:None)
  self.assertFalse(service.start());self.assertFalse(service.status()['running'])
 def test_sandbox_simulates_events_no_network_and_no_route_file(self):
  script=os.path.join(self.d.name,'case.lua')
  with open(script,'w') as f:f.write("function on_load(e) br.after(1,'on_custom',{}) end\nfunction on_custom(e) br.route_save('bad',{{x=1,y=2,z=3}});br.chat('/x');br.log('done') end")
  lines=[]
  run('case.lua',root=self.d.name,output=lines.append)
  self.assertTrue(any('done' in x for x in lines));self.assertTrue(any('имитация' in x for x in lines))
  self.assertFalse(os.path.exists(os.path.join(self.d.name,'bad.json')))
 def test_vpn_use_nondirect_starts_bridge(self):
  from bot import handle_vpn_command
  from types import SimpleNamespace
  node=SimpleNamespace(is_direct=False,protocol='vless',name='example')
  class Pool:
   def find(self, name):return SimpleNamespace(node=node,usable=True,reason='')
   def socks_url_for(self, requested):
    self.called=requested
    return 'socks5://127.0.0.1:22222'
  class Manager:
   def __init__(self):self.proxy_pool=Pool();self.received=None
   def apply_proxy_to_bots(self, proxy, ids):self.received=proxy;return {'ok':True,'applied':[1]}
  manager=Manager()
  with contextlib.redirect_stdout(io.StringIO()):handle_vpn_command(manager, '/vpn use example all')
  self.assertIs(manager.proxy_pool.called,node)
  self.assertEqual(manager.received,'socks5://127.0.0.1:22222')
 def test_sandbox_rejects_outside_path(self):
  with self.assertRaises(ValueError):resolve('../case.lua','.lua',self.d.name)
if __name__=='__main__':unittest.main()
