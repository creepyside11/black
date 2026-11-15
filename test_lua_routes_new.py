import json, os, tempfile, time, unittest
from logic.lua_routes import RouteController, load_route, save_route
from logic.events import emitter
from logic.lua_runtime import LuaRuntime

class FakeAPI:
    def __init__(self):
        self.session=type('Session', (), {'connected':True})()
        self.moves=[]; self.stops=[]; self.position=(0,0,0)
    def getbotposition(self):return self.position
    def MoveToCoord(self,x,y,z,**kw):
        self.moves.append((x,y,z,kw)); return len(self.moves)
    def StopMoveToCoord(self,movement_id=None):self.stops.append(movement_id);return True
class Bot:
    def __init__(self):
        self.bot_id=8001; self.api=FakeAPI();self.session=self.api.session;self.logs=[]
    def add_log(self, msg):self.logs.append(msg)
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=self.tmp.name;self.bot=Bot();self.now=0;self.events=[]
        save_route('path',[[1,2,3],{'x':4,'y':5,'z':6,'radius':3,'wait':2}],self.root)
        self.c=RouteController(self.bot,lambda name,data:self.events.append((name,data)),self.bot.add_log,armed=True,root=self.root,clock=lambda:self.now)
    def tearDown(self): self.tmp.cleanup()
    def test_waypoints_wait_and_unrelated_movement(self):
        self.c.start('path');self.c.on_move_end({'movement_id':999},'arrived')
        self.assertEqual(len(self.bot.api.moves),1)
        self.c.on_move_end({'movement_id':1},'arrived');self.c.tick()
        self.assertEqual(len(self.bot.api.moves),1)
        self.c.point_pending=False;self.c.tick();self.assertEqual(len(self.bot.api.moves),2)
        self.c.on_move_end({'movement_id':2},'arrived');self.c.point_pending=False;self.now=1;self.c.tick()
        self.assertEqual(self.c.state,'waiting');self.now=2;self.c.tick()
        self.assertEqual(self.c.state,'finished')
        self.assertEqual([x[0] for x in self.events],['on_route_start_point','on_route_point','on_route_start_point','on_route_point','on_route_end'])
    def test_pause_resume_stop(self):
        self.c.start('path');self.c.pause();self.assertEqual(self.bot.api.stops,[1]);self.c.resume()
        self.assertEqual(len(self.bot.api.moves),2);self.c.stop();self.assertEqual(self.bot.api.stops,[1,2])
    def test_wrong_status_stuck_and_disconnect(self):
        self.c.start('path');self.c.on_move_end({'movement_id':1},'cancelled');self.assertEqual(self.c.state,'error')
        self.c.start('path',stuck_timeout=2);self.now=2;self.c.tick();self.assertEqual(self.c.state,'error');self.assertIn('застревание',self.events[-1][1]['reason'])
        self.c.start('path');self.bot.api.session.connected=False;self.c.tick();self.assertEqual(self.c.state,'error')
    def test_dry_loads_without_move(self):
        c=RouteController(self.bot,lambda *a:None,self.bot.add_log,armed=False,root=self.root)
        c.start('path');self.assertEqual(c.state,'dry');self.assertFalse(self.bot.api.moves)
    def test_bad_route_is_not_saved(self):
        with self.assertRaises(ValueError):save_route('bad',[[1,2,float('nan')]],self.root)
        with self.assertRaises(ValueError):load_route('../escape',self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root,'bad.json')))

class RouteRuntimeTest(unittest.TestCase):
    def test_lua_saves_and_dry_does_not_save(self):
        bot=Bot()
        with tempfile.TemporaryDirectory() as d:
            script=os.path.join(d,'save.lua')
            with open(script,'w') as f:f.write("function on_load(e) br.route_save('saved', {{x=1,y=2,z=3}}) end")
            for armed in (False,True):
                runtime=LuaRuntime(bot,script,armed=armed,tick=.05);runtime.route.root=d
                try:
                    runtime.start();end=time.monotonic()+2
                    while time.monotonic()<end and sum(runtime.script.stats[k] for k in ('sent','skipped','errors')) == 0:time.sleep(.01)
                    self.assertEqual(os.path.exists(os.path.join(d,'saved.json')),armed)
                    if armed:self.assertEqual(load_route('saved',d)[0]['x'],1)
                finally:runtime.stop()

    def test_lua_starts_moves_receives_events_and_stops(self):
        bot=Bot()
        with tempfile.TemporaryDirectory() as d:
            save_route('path',[[1,2,3],[4,5,6]],d)
            script=os.path.join(d,'test.lua')
            with open(script,'w') as f:f.write('''
function on_load(e) br.route_start('path','walk',{timeout=5,stuck_timeout=5}) end
function on_route_point(e) br.log('POINT='..e.point) end
function on_route_end(e) br.log('DONE') end
''')
            runtime=LuaRuntime(bot,script,armed=True,tick=.05)
            runtime.route.root=d
            try:
                runtime.start()
                def until(f):
                    end=time.monotonic()+3
                    while time.monotonic()<end:
                        if f():return True
                        time.sleep(.01)
                    return False
                self.assertTrue(until(lambda:len(bot.api.moves)==1),bot.logs)
                emitter.emit('OnCoordMoveEnd',bot.api,{'movement_id':1},'arrived')
                self.assertTrue(until(lambda:len(bot.api.moves)==2),bot.logs)
                emitter.emit('OnCoordMoveEnd',bot.api,{'movement_id':2},'arrived')
                self.assertTrue(until(lambda:any('DONE' in x for x in bot.logs)),bot.logs)
                self.assertTrue(any('POINT=1' in x for x in bot.logs))
                self.assertEqual(runtime.route.state,'finished')
            finally:runtime.stop()

if __name__=='__main__':unittest.main()
