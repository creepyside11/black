"""Мост на живом sing-box: поднять, отдать SOCKS5, погасить.

Тест пропускается, если sing-box не установлен — на телефоне его нет,
на VPS он ставится _diag/vps/install_singbox.sh.
"""

import socket
import time
import unittest

from logic import vpn_bridge as B

VLESS = ("vless://11111111-2222-3333-4444-555555555555@127.0.0.1:9"
         "?type=tcp&security=none#Test")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(B.bridge_available(), "sing-box не установлен")
class LiveBridgeTests(unittest.TestCase):
    def test_bridge_opens_socks_port(self):
        bridge = B.VpnBridge(binary=B.find_binary())
        key = "test://live"
        port = free_port()
        # Подменяем порт, чтобы не конфликтовать с боевыми.
        B.port_for = lambda _key: port
        try:
            url = bridge.start(key, VLESS)
            self.assertIsNotNone(url, "мост не поднялся")
            self.assertEqual(url, f"socks5://127.0.0.1:{port}")
            self.assertTrue(B.socks_alive(port))
            self.assertIn(key, bridge.running())
        finally:
            bridge.stop_all()
        time.sleep(0.5)
        self.assertFalse(B.socks_alive(port), "порт остался открытым")

    def test_bad_config_does_not_hang(self):
        bridge = B.VpnBridge(binary=B.find_binary())
        started = time.monotonic()
        url = bridge.start("bad://key", "не ссылка вовсе")
        elapsed = time.monotonic() - started
        self.assertIsNone(url)
        self.assertLess(elapsed, 10.0, "мост завис на битом конфиге")
        bridge.stop_all()


if __name__ == "__main__":
    unittest.main(verbosity=2)
