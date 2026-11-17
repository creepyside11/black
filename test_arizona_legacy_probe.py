import socket
import struct
import threading
import unittest

from tools.arizona_legacy_probe import probe


class LegacyProbeTests(unittest.TestCase):
    def server(self, fn):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1', 0))
        sock.settimeout(1)
        thread = threading.Thread(target=fn, args=(sock,), daemon=True)
        thread.start()
        self.addCleanup(sock.close)
        self.addCleanup(thread.join, 1)
        return sock.getsockname()[1]

    def test_cookie_exchange(self):
        packets = []
        def handler(sock):
            data, peer = sock.recvfrom(512); packets.append(data)
            sock.sendto(b'\x1a\x34\x12', peer)
            data, peer = sock.recvfrom(512); packets.append(data)
            sock.sendto(b'\x19\x00', peer)
        port = self.server(handler)
        result = probe('127.0.0.1', port, 1)
        self.assertEqual(result['status'], 'offline_reply')
        self.assertTrue(result['cookie'])
        self.assertEqual(packets, [b'\x18\x00\x00', b'\x18' + struct.pack('<H', 0x1234 ^ 0x6969)])

    def test_direct_reply(self):
        def handler(sock):
            _, peer = sock.recvfrom(512); sock.sendto(b'\x19\x00', peer)
        self.assertEqual(probe('127.0.0.1', self.server(handler), 1)['status'], 'offline_reply')

    def test_timeout_and_invalid_reply(self):
        def handler(sock):
            _, peer = sock.recvfrom(512); sock.sendto(b'\x19', peer)
        self.assertEqual(probe('127.0.0.1', self.server(handler), .1)['status'], 'timeout')

    def test_invalid_args(self):
        with self.assertRaises(ValueError): probe('127.0.0.1', 0)
        with self.assertRaises(ValueError): probe('127.0.0.1', 7777, 31)


class LuaDiagnosticsTests(unittest.TestCase):
    def test_examples_with_simulated_events(self):
        from pathlib import Path
        import tempfile
        import time
        from unittest import mock
        from logic import lua_storage
        from logic.events import emitter
        from logic.lua_engine import find_lua
        from logic.lua_runtime import LuaRuntime
        from test_lua_integration import Bot, until
        if not find_lua():
            self.skipTest('Lua 5.4 unavailable')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(lua_storage, 'ROOT', tmp):
            for name in ('arizona_connection_diagnostic.lua', 'arizona_dialog_diagnostic.lua'):
                bot = Bot(1)
                path = Path('test_lua_scripts') / name
                runtime = LuaRuntime(bot, str(path), tick=.05).start()
                try:
                    self.assertTrue(until(lambda: len(bot.logs) > 0), runtime.failure)
                    if 'dialog' in name:
                        emitter.emit('onShowDialog', bot.api, 42, 1, 'secret title', 'secret text', 'yes', 'no')
                        self.assertTrue(until(lambda: any('id=42' in x for x in bot.logs)), runtime.failure)
                        self.assertFalse(any('secret' in x for x in bot.logs))
                    else:
                        emitter.emit('onSpawn', bot.api)
                        self.assertTrue(until(lambda: any('спавна' in x for x in bot.logs)), runtime.failure)
                finally:
                    runtime.stop()

if __name__ == '__main__':
    unittest.main()
