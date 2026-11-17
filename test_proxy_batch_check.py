"""Test batch checks without accessing proxies or changing active sessions."""
import socket
import struct
import threading
import unittest
from unittest import mock
from logic import proxy_batch_check as check
from logic.vpn_sub import VpnNode

class ParseAndBatchTests(unittest.TestCase):
    def test_public_ipv4_socks_only_deduped(self):
        nodes=check.parse_proxies('8.8.8.8:1080\nsocks5://8.8.8.8:1080\n127.0.0.1:1080\nhttp://8.8.8.8:3128\n10.0.0.1:80\n')
        self.assertEqual(len(nodes),1); self.assertEqual(nodes[0].address,'8.8.8.8')
        with self.assertRaises(ValueError):check.parse_proxies('127.0.0.1:1080')
    def test_tcp_and_udp_are_distinct_and_bounded(self):
        nodes=check.parse_proxies('8.8.8.8:1080\n1.1.1.1:1080')
        with mock.patch.object(check,'_tcp_carries',return_value=(True,'')), mock.patch.object(check,'check_udp',side_effect=[True,False]):
            results=check.check_many(nodes,workers=1)
        self.assertEqual([(tcp,udp) for _,tcp,udp in results],[(True,True),(True,False)])
    def test_udp_not_tested_when_tcp_fails(self):
        node=check.parse_proxies('8.8.8.8:1080')[0]
        with mock.patch.object(check,'_tcp_carries',return_value=(False,'')), mock.patch.object(check,'check_udp') as udp:
            self.assertEqual(check.check_one(node)[1:],(False,False)); udp.assert_not_called()
    def test_worker_override_capped(self):
        with mock.patch.dict(check.os.environ,{'BR_PROXY_CHECK_WORKERS':'1000'}):self.assertEqual(check.worker_count(),128)

class LocalSocksUDPTests(unittest.TestCase):
    def test_udp_stun_requires_matching_transaction(self):
        relay=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);relay.bind(('127.0.0.1',0));relay.settimeout(2)
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);server.settimeout(2)
        def work():
            try:
                conn,_=server.accept()
                with conn:
                    conn.settimeout(2); conn.recv(3);conn.sendall(b'\x05\x00');conn.recv(10)
                    conn.sendall(b'\x05\x00\x00\x01'+socket.inet_aton('127.0.0.1')+struct.pack('>H',relay.getsockname()[1]))
                    request,addr=relay.recvfrom(2048)
                    body=b'\x01\x01\x00\x00'+struct.pack('>I',check.STUN_MAGIC)+request[18:30]
                    relay.sendto(request[:10]+body,addr)
            finally:
                server.close(); relay.close()
        t=threading.Thread(target=work);t.start()
        try:
            node=VpnNode(name='local',protocol='socks5',address='127.0.0.1',port=server.getsockname()[1])
            with mock.patch.object(check.socket,'gethostbyname',return_value='127.0.0.1'):
                self.assertTrue(check.check_udp(node,2))
        finally:t.join(3)


class TelegramBatchTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from test_telegram_lua import FakeTelegram
        import tg_controller
        self.NS=SimpleNamespace
        self.ctrl=tg_controller.TelegramBotController.__new__(tg_controller.TelegramBotController)
        self.ctrl.admin_ids=[42];self.ctrl.user_states={};self.ctrl.bot=FakeTelegram()
        self.ctrl.manager=SimpleNamespace(_lock=threading.RLock(),bots={},settings=None)
        self.ctrl._setup_handlers()
    def click(self, uid=42, chat_id=42, chat_type='private'):
        self.ctrl.bot.callback(self.NS(data='proxy_batch',id='q',from_user=self.NS(id=uid),message=self.NS(chat=self.NS(id=chat_id,type=chat_type),message_id=1)))
    def test_private_admin_gate_and_input(self):
        self.click(7,7);self.click(42,-10,'supergroup');self.assertFalse(self.ctrl.user_states)
        self.click();self.assertEqual(self.ctrl.user_states[42]['action'],'proxy_batch_input')
        message=self.NS(chat=self.NS(id=42,type='private'),from_user=self.NS(id=42),text='8.8.8.8:1080')
        with mock.patch.object(self.ctrl,'_start_proxy_batch',return_value=True) as start:
            self.ctrl.bot.handlers['handle_user_text'](message)
            start.assert_called_once_with(42,'8.8.8.8:1080')
        self.assertNotIn(42,self.ctrl.user_states)
    def test_txt_upload(self):
        self.click();message=self.NS(chat=self.NS(id=42,type='private'),from_user=self.NS(id=42),document=self.NS(file_name='proxy.txt',file_size=8,file_id='id'))
        self.ctrl.bot.download_file=lambda _:b'8.8.8.8:1080'
        with mock.patch.object(self.ctrl,'_start_proxy_batch',return_value=True) as start:
            self.ctrl.bot.handlers['handle_lua_document'](message)
            start.assert_called_once_with(42,'8.8.8.8:1080')
    def test_batch_sends_results_without_touching_accounts(self):
        node=check.parse_proxies('8.8.8.8:1080')[0]
        account=self.NS(proxy='socks5://old:1080',stop=mock.Mock())
        self.ctrl.manager.bots[1]=account
        docs=[]
        self.ctrl.bot.send_document=lambda chat,doc,**kw:docs.append((doc.name,doc.read().decode()))
        class ImmediateThread:
            def __init__(self, target, **kwargs):self.target=target
            def start(self):self.target()
        with mock.patch.object(check,'check_many',return_value=[(node,True,True)]), mock.patch('tg_controller.threading.Thread',ImmediateThread):
            self.assertTrue(self.ctrl._start_proxy_batch(42,'8.8.8.8:1080'))
        self.assertEqual([item[0] for item in docs],['proxy_tcp.txt','proxy_udp.txt'])
        self.assertEqual(docs[1][1],'socks5://8.8.8.8:1080\n')
        self.assertEqual(account.proxy,'socks5://old:1080');account.stop.assert_not_called()

if __name__ == "__main__": unittest.main()
