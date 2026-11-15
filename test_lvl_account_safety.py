"""Offline regression for reconnect, world state and palette detection."""
import unittest
from unittest import mock
from types import SimpleNamespace
import bot as botmod
from logic.events import EventEmitter
from logic import real_world, lvl_engine
from logic.bot_tasks import LevelRunnerTask
from logic.quest_helpers import QuestHelpers

class AccountEventTests(unittest.TestCase):
    def setUp(self):
        self.emitter=EventEmitter(max_workers=2)
        self.patcher=mock.patch.object(botmod,'emitter',self.emitter);self.patcher.start()
        self.mgr=botmod.BotManager.__new__(botmod.BotManager)
        self.bot=SimpleNamespace(bot_id=1,nickname='Test_User',auto_regen_nick=True,logs=[],
            add_log=lambda text:self.bot.logs.append(text),status='',schedule_reconnect=mock.Mock(),
            begin_spawn_watchdog=mock.Mock())
        self.mgr._find_bot_by_session_or_api=mock.Mock(return_value=self.bot)
        self.mgr.replace_bot_nickname=mock.Mock();self.mgr._setup_event_hooks()
    def tearDown(self):self.emitter.shutdown();self.patcher.stop()
    def call(self,name,*args):
        handler=[f for f in self.emitter._listeners[name] if not f.__name__.startswith('_default')][-1]
        handler(object(),*args)
    def test_existing_nick_login_does_not_replace_or_remove(self):
        self.call('onRegistrationState','logging_in')
        self.assertFalse(self.bot.auto_regen_nick);self.mgr.replace_bot_nickname.assert_not_called()
        self.assertEqual(self.bot.nickname,'Test_User')
    def test_new_registration_claims_nick_before_later_login(self):
        self.call('onRegistrationState','registering');self.call('onRegistrationState','logging_in')
        self.assertFalse(self.bot.auto_regen_nick);self.mgr.replace_bot_nickname.assert_not_called()
    def test_kick_two_waits_even_for_generated_nick(self):
        self.call('onKick',2)
        self.mgr.replace_bot_nickname.assert_not_called()
        self.assertTrue(self.bot.schedule_reconnect.call_args.kwargs['force'])
        self.assertGreaterEqual(self.bot.schedule_reconnect.call_args.kwargs['delay'],45)
        self.assertEqual(self.bot.nickname,'Test_User')

class WorldTests(unittest.TestCase):
    def test_disconnect_during_wait_is_not_world_unconfirmed(self):
        session=SimpleNamespace(connected=True)
        b=SimpleNamespace(session=session,api=SimpleNamespace(getbotposition=lambda:(10,20,3)))
        def disconnected(*args,**kwargs):session.connected=False;return 'offline'
        with mock.patch.object(real_world,'_wait_spawn_allowed',side_effect=disconnected):
            result=real_world.ensure(b,timeout=0.01,enter_timeout=0.01,fallback_warp=False)
        self.assertEqual(result['status'],'disconnected')
    def test_lvl_disconnect_does_not_report_empty_world(self):
        b=SimpleNamespace(session=SimpleNamespace(connected=True),lvl_phase='',_lvl_engine_active=False)
        messages=[]
        with mock.patch.object(real_world,'ensure',return_value={'status':'disconnected','players':[]}):
            result=lvl_engine.run_cycle(b,log=messages.append)
        self.assertEqual(result['status'],'disconnected');self.assertTrue(any('соединение разорвалось' in m for m in messages))

class PaletteTests(unittest.TestCase):
    def test_unknown_numeric_list_is_not_paint(self):
        logs=[];h=QuestHelpers(SimpleNamespace(),log=logs.append)
        payload={'i':[{'id':n,'n':str(n)} for n in range(1300,1440)]}
        self.assertIsNone(h.on_json(999,payload));self.assertFalse(any('палитра' in x for x in logs))
    def test_real_named_palette_is_still_recognized(self):
        h=QuestHelpers(SimpleNamespace(),log=lambda m:None)
        self.assertEqual(h.on_json(91,{'c':[{'id':2,'n':'Чёрный'}]}),'paint')

class HoldTests(unittest.TestCase):
    def test_hold_aborts_when_session_changes(self):
        old=SimpleNamespace(connected=True)
        b=SimpleNamespace(session=old,api=SimpleNamespace(getbotposition=mock.Mock()),nickname='Test_User',bot_id=1,add_log=mock.Mock())
        task=LevelRunnerTask(b);task.running=True;task.HOLD_POLL=0.01
        def replace_session(_):b.session=SimpleNamespace(connected=True)
        with mock.patch('logic.bot_tasks.time.sleep',side_effect=replace_session),mock.patch('logic.lvl_engine.travel_to_point') as move:
            task._hold({'target':[100,200,5],'station':'Южный'})
        move.assert_not_called();b.api.getbotposition.assert_not_called()
