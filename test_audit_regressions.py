"""Offline regressions for proxy selection: no network or real accounts."""
import contextlib, io, threading, unittest
from unittest.mock import Mock
from types import SimpleNamespace
from bot import BotManager, BotInstance, _parse_bot_ids, handle_vpn_command, handle_proxy_command

class ProxySelectionTests(unittest.TestCase):
    def setUp(self):
        self.manager=BotManager.__new__(BotManager)
        self.manager._lock=threading.RLock()
        self.manager.bots={1:SimpleNamespace(bot_id=1,proxy='old',proxy_node='old-node'),2:SimpleNamespace(bot_id=2,proxy='old2',proxy_node='node2')}
        self.manager.proxy_pool=Mock()
        self.manager.proxy_pool.verify_before_connect.return_value=(True,'ok')
        self.manager.proxy_pool.node_for_proxy.return_value=None
        self.manager.save_accounts=Mock()
    def test_failed_proxy_preparation_never_starts_thread(self):
        b = SimpleNamespace(is_banned=False, check_proxy_before_start=lambda:(True,''),
            _hold_proxy_bridge=Mock(), _release_proxy_bridge=Mock(), cancel_reconnect=Mock(),
            add_log=Mock(), setup_session=Mock(return_value=False), thread=None)
        self.assertFalse(BotInstance.start(b))
        self.assertIsNone(b.thread)
        b._release_proxy_bridge.assert_called_once()
        self.assertEqual(b.status, 'Ошибка настройки подключения')
    def test_invalid_explicit_proxy_does_not_create_direct_account(self):
        self.manager.settings=None
        self.manager.resolve_proxy=Mock(return_value='')
        with contextlib.redirect_stdout(io.StringIO()):
            result=self.manager.add_bot('SERVER','Name','password',proxy='vless://invalid')
        self.assertIsNone(result)
        self.assertEqual(len(self.manager.bots),2)
    def test_invalid_ids_not_all(self):
        for x in ('abc','1,oops','1,','-1'):
            with self.subTest(x=x):
                with self.assertRaises(ValueError):_parse_bot_ids(x,self.manager)
        self.assertIsNone(_parse_bot_ids('all',self.manager))
        self.assertEqual(_parse_bot_ids('1,2,1',self.manager),[1,2])
    def test_vpn_command_does_not_apply_bad_ids(self):
        self.manager.proxy_pool.find=Mock()
        with contextlib.redirect_stdout(io.StringIO()):handle_vpn_command(self.manager,'/vpn use example typo')
        self.manager.proxy_pool.find.assert_not_called()
        self.assertEqual(self.manager.bots[1].proxy,'old')
    def test_empty_list_never_means_everyone(self):
        report=self.manager.apply_proxy_to_bots('socks5://127.0.0.1:12345',[])
        self.assertFalse(report['ok']);self.assertEqual(self.manager.bots[1].proxy,'old')
    def test_unknown_id_reports_error(self):
        report=self.manager.apply_proxy_to_bots('socks5://127.0.0.1:12345',[99])
        self.assertFalse(report['ok']);self.assertEqual(self.manager.bots[2].proxy,'old2')
    def test_clearing_proxy_clears_saved_node(self):
        self.assertTrue(self.manager.set_bot_proxy(1,'none'))
        self.assertEqual((self.manager.bots[1].proxy,self.manager.bots[1].proxy_node),('',''))
    def test_clear_all_clears_saved_nodes(self):
        with contextlib.redirect_stdout(io.StringIO()):handle_proxy_command(self.manager,'/proxy all none')
        for b in self.manager.bots.values():self.assertEqual((b.proxy,b.proxy_node),('',''))

if __name__=='__main__':unittest.main()
