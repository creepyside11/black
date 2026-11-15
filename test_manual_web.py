import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from logic.manual_web import ManualWeb
from logic.events import emitter

class ManualTest(unittest.TestCase):
 def setUp(self):
  self.session=SimpleNamespace(connected=False,send_dialog_response=Mock(return_value=True),send_json=Mock(return_value=True))
  self.api=SimpleNamespace(sendchat=Mock())
  self.bot=SimpleNamespace(bot_id=1,session=self.session,api=self.api,thread=None,active_task=None,lua_runtime=None,status='Тест',stop=Mock())
  def start():self.session.connected=True;return True
  self.bot.start=start
  self.manager=SimpleNamespace(_find_bot_by_session_or_api=lambda source:self.bot if source is self.api else None,refresh_bridge_proxy=Mock())
  self.web=ManualWeb(self.manager);self.web.begin(self.bot)
 def tearDown(self):self.web.close()
 def test_chat_and_dialog_with_stale_response(self):
  emitter.emit('onShowChat',self.api,'Привет')
  emitter.emit('onShowDialog',self.api,7,0,'Окно','Текст','Да','Нет')
  state=self.web.state(self.bot);self.assertEqual(state['chat'][0]['text'],'Привет')
  self.assertEqual(state['window']['id'],7)
  with self.assertRaises(ValueError):self.web.answer(self.bot,{'token':'old'})
  self.web.answer(self.bot,{'token':state['window']['token'],'response':1,'item':0,'text':'ok'})
  self.session.send_dialog_response.assert_called_once_with(7,1,0,'ok')
  self.assertIsNone(self.web.state(self.bot)['window'])
  with self.assertRaises(ValueError):self.web.answer(self.bot,{'token':state['window']['token']})
 def test_spawn_requires_list_and_matching_choice(self):
  emitter.emit('onSpawnSelect',self.api,{'m':[3,5]})
  window=self.web.state(self.bot)['window']
  with self.assertRaises(ValueError):self.web.answer(self.bot,{'token':window['token'],'choice':9})
  with patch('logic.spawn.select_spawn',return_value=True) as select:
   self.web.answer(self.bot,{'token':window['token'],'choice':5});self.assertEqual(select.call_args.args[1],5)
 def test_chat_and_disconnect(self):
  self.web.chat(self.bot,'/mm');self.api.sendchat.assert_called_once_with('/mm')
  emitter.emit('disconnect',self.api)
  self.assertIn('Соединение прервано',self.web.state(self.bot)['chat'][-1]['text'])
 def test_foreign_source_ignored(self):
  emitter.emit('onShowChat',object(),'private');self.assertEqual(self.web.state(self.bot)['chat'],[])
 def test_npc_button(self):
  emitter.emit('onNpcDialog',self.api,'Охранник','Привет',1,[{'key':4,'text':'Да'}])
  w=self.web.state(self.bot)['window'];self.web.answer(self.bot,{'token':w['token'],'choice':4})
  self.session.send_json.assert_called_once_with(63,{'bk':4},packet_name='interface_npc_dialog_response')
if __name__=='__main__':unittest.main()
