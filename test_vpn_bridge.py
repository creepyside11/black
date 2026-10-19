"""Мост VLESS -> SOCKS5: разбор ссылок и сборка конфига sing-box.

Без этого модуля узлы из подписки (vless/vmess/trojan/ss) оставались
жёлтыми — «живой, но нужен внешний клиент» — и применить их было нельзя.
"""

import json
import unittest

from logic import vpn_bridge as B

VLESS_REALITY = (
    "vless://11111111-2222-3333-4444-555555555555@deu.babay.su:443"
    "?type=tcp&security=reality&pbk=PUBKEY123&sid=ab12&fp=chrome"
    "&sni=www.microsoft.com&flow=xtls-rprx-vision#%F0%9F%87%A9%F0%9F%87%AAGermany"
)
VLESS_WS = (
    "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@example.com:8443"
    "?type=ws&security=tls&sni=example.com&path=%2Fws&host=example.com#Node"
)
TROJAN = "trojan://secretpass@trojan.example.com:443?sni=trojan.example.com#Tr"
SS = "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@ss.example.com:8388#Ss"


class ParseVlessTests(unittest.TestCase):
    def test_reality_node(self):
        out = B.parse_link(VLESS_REALITY)
        self.assertEqual(out["type"], "vless")
        self.assertEqual(out["server"], "deu.babay.su")
        self.assertEqual(out["server_port"], 443)
        self.assertEqual(out["uuid"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(out["flow"], "xtls-rprx-vision")
        self.assertTrue(out["tls"]["enabled"])
        self.assertEqual(out["tls"]["server_name"], "www.microsoft.com")
        self.assertEqual(out["tls"]["reality"]["public_key"], "PUBKEY123")
        self.assertEqual(out["tls"]["reality"]["short_id"], "ab12")
        self.assertEqual(out["tls"]["utls"]["fingerprint"], "chrome")

    def test_ws_transport(self):
        out = B.parse_link(VLESS_WS)
        self.assertEqual(out["transport"]["type"], "ws")
        self.assertEqual(out["transport"]["path"], "/ws")
        self.assertEqual(out["transport"]["headers"]["Host"], "example.com")

    def test_no_tls_means_no_tls_block(self):
        out = B.parse_link("vless://uuid@host.com:80?type=tcp&security=none#X")
        self.assertNotIn("tls", out)

    def test_broken_link_returns_none(self):
        self.assertIsNone(B.parse_link("vless://без-собаки"))
        self.assertIsNone(B.parse_link(""))
        self.assertIsNone(B.parse_link("ftp://nope"))


class ParseOtherTests(unittest.TestCase):
    def test_trojan(self):
        out = B.parse_link(TROJAN)
        self.assertEqual(out["type"], "trojan")
        self.assertEqual(out["password"], "secretpass")
        self.assertTrue(out["tls"]["enabled"])

    def test_shadowsocks_base64(self):
        out = B.parse_link(SS)
        self.assertEqual(out["type"], "shadowsocks")
        self.assertEqual(out["method"], "aes-256-gcm")
        self.assertEqual(out["password"], "password")
        self.assertEqual(out["server"], "ss.example.com")
        self.assertEqual(out["server_port"], 8388)

    def test_vmess(self):
        payload = {
            "add": "vm.example.com", "port": "443", "id": "uuid-here",
            "aid": "0", "net": "ws", "path": "/ray", "host": "vm.example.com",
            "tls": "tls", "sni": "vm.example.com",
        }
        import base64
        link = "vmess://" + base64.b64encode(json.dumps(payload).encode()).decode()
        out = B.parse_link(link)
        self.assertEqual(out["type"], "vmess")
        self.assertEqual(out["uuid"], "uuid-here")
        self.assertEqual(out["transport"]["path"], "/ray")


class PortTests(unittest.TestCase):
    def test_port_is_stable(self):
        key = "vless://deu.babay.su:443"
        self.assertEqual(B.port_for(key), B.port_for(key))

    def test_ports_differ_for_different_nodes(self):
        ports = {B.port_for(f"vless://host{i}.com:443") for i in range(40)}
        self.assertGreater(len(ports), 35, "порты слишком часто совпадают")

    def test_port_in_range(self):
        for i in range(50):
            port = B.port_for(f"node{i}")
            self.assertGreaterEqual(port, B.PORT_BASE)
            self.assertLess(port, B.PORT_BASE + B.PORT_SPAN)


class PortAllocationTests(unittest.TestCase):
    """Порт узла считается хешем ключа, поэтому у двух узлов он совпадает.

    Второй мост не мог занять порт, но socks_alive() видел чужой
    слушающий сокет и рапортовал успех — узел «работал» через туннель
    соседа. На живом прогоне из 12 узлов три пары так и слиплись.
    """

    def setUp(self):
        self.bridge = B.VpnBridge(binary="/bin/true")
        # Ничего не слушает: тест про арифметику, а не про сеть.
        self._orig_alive = B.socks_alive
        B.socks_alive = lambda port, timeout=1.0: False

    def tearDown(self):
        B.socks_alive = self._orig_alive

    def test_same_key_gets_same_port(self):
        first = self.bridge._alloc_port("node-a")
        self.assertEqual(self.bridge._alloc_port("node-a"), first)

    def test_colliding_keys_get_different_ports(self):
        a = self.bridge._alloc_port("node-a")
        # Подсовываем ключ, чей хеш даёт тот же порт.
        self.bridge._ports["node-b"] = a
        b = self.bridge._alloc_port("node-c")
        self.assertNotEqual(a, b)

    def test_many_keys_all_unique(self):
        ports = {self.bridge._alloc_port(f"node-{i}") for i in range(200)}
        self.assertEqual(len(ports), 200, "порты повторились")

    def test_busy_port_is_skipped(self):
        base = B.port_for("node-x")
        B.socks_alive = lambda port, timeout=1.0: port == base
        port = self.bridge._alloc_port("node-x")
        self.assertNotEqual(port, base)

    def test_kill_frees_port(self):
        port = self.bridge._alloc_port("node-a")
        self.bridge._kill("node-a")
        self.assertNotIn("node-a", self.bridge._ports)


class ConfigTests(unittest.TestCase):
    def test_config_has_socks_inbound_and_outbound(self):
        out = B.parse_link(VLESS_REALITY)
        cfg = B.build_config("key", out, 20123)
        self.assertEqual(cfg["inbounds"][0]["type"], "socks")
        self.assertEqual(cfg["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 20123)
        self.assertEqual(cfg["outbounds"][0]["type"], "vless")
        self.assertEqual(cfg["outbounds"][0]["tag"], "out")
        self.assertEqual(cfg["route"]["final"], "out")

    def test_config_is_json_serializable(self):
        cfg = B.build_config("k", B.parse_link(VLESS_WS), 20200)
        text = json.dumps(cfg, ensure_ascii=False)
        self.assertIn("socks", text)


class NoBinaryTests(unittest.TestCase):
    """Без sing-box мост не притворяется работающим."""

    def test_unavailable_without_binary(self):
        bridge = B.VpnBridge(binary="/nonexistent/sing-box")
        self.assertFalse(bridge.available())
        self.assertIsNone(bridge.start("k", VLESS_REALITY))

    def test_running_empty(self):
        self.assertEqual(B.VpnBridge(binary="/nonexistent/x").running(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
