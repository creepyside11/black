"""Конвертер Xray/V2Ray JSON -> outbound sing-box.

Подписка green.babay.su при запросе с клиентским User-Agent отдаёт не
ссылки vless://, а готовые конфиги Xray. Ссылочный разбор на них
возвращал None — все 51 узел висели неприменимыми, и мост поднять было
не из чего.
"""

import json
import unittest

from logic import vpn_bridge as B


VLESS_REALITY = {
    "dns": {"servers": ["1.1.1.1", "8.8.8.8", "localhost"]},
    "routing": {"rules": [{"protocol": ["bittorrent"], "outboundTag": "direct"}]},
    "inbounds": [{"tag": "socks", "protocol": "socks", "port": 10808}],
    "outbounds": [
        {
            "tag": "proxy",
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "deu.babay.su",
                        "port": 443,
                        "users": [
                            {
                                "id": "84d2af53-66b1-475a-801c-3acd0178b887",
                                "encryption": "none",
                                "flow": "xtls-rprx-vision",
                            }
                        ],
                    }
                ]
            },
            "streamSettings": {
                "network": "tcp",
                "tcpSettings": {},
                "security": "reality",
                "realitySettings": {
                    "serverName": "deu.babay.su",
                    "publicKey": "rA5KAscL-AJi42uXuNjcu_EiIIrPV5-t0JznBVhHrC8",
                    "shortId": "41b699e1e4e4cacb",
                    "fingerprint": "firefox",
                },
            },
        },
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ],
    "remarks": "🇩🇪 Германия",
}


def vless_ws_tls():
    return {
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": "ws.example.com", "port": 8443,
                "users": [{"id": "11111111-2222-3333-4444-555555555555"}],
            }]},
            "streamSettings": {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {"serverName": "cdn.example.com", "fingerprint": "chrome"},
                "wsSettings": {"path": "/ray", "headers": {"Host": "cdn.example.com"}},
            },
        }]
    }


def vmess_grpc():
    return {
        "outbounds": [{
            "protocol": "vmess",
            "settings": {"vnext": [{
                "address": "vm.example.com", "port": 443,
                "users": [{"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                           "alterId": 0, "security": "auto"}],
            }]},
            "streamSettings": {
                "network": "grpc",
                "security": "tls",
                "tlsSettings": {"serverName": "vm.example.com"},
                "grpcSettings": {"serviceName": "grpcsvc"},
            },
        }]
    }


def trojan_tcp():
    return {
        "outbounds": [{
            "protocol": "trojan",
            "settings": {"servers": [{
                "address": "tj.example.com", "port": 443, "password": "secret",
            }]},
            "streamSettings": {"network": "tcp", "security": "tls",
                               "tlsSettings": {"serverName": "tj.example.com"}},
        }]
    }


def ss_plain():
    return {
        "outbounds": [{
            "protocol": "shadowsocks",
            "settings": {"servers": [{
                "address": "ss.example.com", "port": 8388,
                "method": "aes-256-gcm", "password": "pw",
            }]},
        }]
    }


class VlessRealityTests(unittest.TestCase):
    def setUp(self):
        self.out = B.parse_link(json.dumps(VLESS_REALITY))

    def test_parsed(self):
        self.assertIsNotNone(self.out, "конфиг не разобран")

    def test_type_and_address(self):
        self.assertEqual(self.out["type"], "vless")
        self.assertEqual(self.out["server"], "deu.babay.su")
        self.assertEqual(self.out["server_port"], 443)

    def test_uuid_and_flow(self):
        self.assertEqual(self.out["uuid"], "84d2af53-66b1-475a-801c-3acd0178b887")
        self.assertEqual(self.out["flow"], "xtls-rprx-vision")

    def test_reality(self):
        tls = self.out["tls"]
        self.assertTrue(tls["enabled"])
        self.assertEqual(tls["server_name"], "deu.babay.su")
        self.assertTrue(tls["reality"]["enabled"])
        self.assertEqual(tls["reality"]["public_key"],
                         "rA5KAscL-AJi42uXuNjcu_EiIIrPV5-t0JznBVhHrC8")
        self.assertEqual(tls["reality"]["short_id"], "41b699e1e4e4cacb")
        self.assertEqual(tls["utls"]["fingerprint"], "firefox")

    def test_no_transport_for_tcp(self):
        self.assertNotIn("transport", self.out)

    def test_direct_and_block_skipped(self):
        """freedom/blackhole — не узлы, их брать нельзя."""
        self.assertEqual(self.out["type"], "vless")


class OtherProtocolTests(unittest.TestCase):
    def test_vless_ws_tls(self):
        out = B.parse_link(json.dumps(vless_ws_tls()))
        self.assertEqual(out["transport"]["type"], "ws")
        self.assertEqual(out["transport"]["path"], "/ray")
        self.assertEqual(out["transport"]["headers"]["Host"], "cdn.example.com")
        self.assertEqual(out["tls"]["server_name"], "cdn.example.com")
        self.assertNotIn("reality", out["tls"])

    def test_vmess_grpc(self):
        out = B.parse_link(json.dumps(vmess_grpc()))
        self.assertEqual(out["type"], "vmess")
        self.assertEqual(out["security"], "auto")
        self.assertEqual(out["alter_id"], 0)
        self.assertEqual(out["transport"]["type"], "grpc")
        self.assertEqual(out["transport"]["service_name"], "grpcsvc")

    def test_trojan(self):
        out = B.parse_link(json.dumps(trojan_tcp()))
        self.assertEqual(out["type"], "trojan")
        self.assertEqual(out["password"], "secret")
        self.assertTrue(out["tls"]["enabled"])

    def test_shadowsocks(self):
        out = B.parse_link(json.dumps(ss_plain()))
        self.assertEqual(out["type"], "shadowsocks")
        self.assertEqual(out["method"], "aes-256-gcm")
        self.assertEqual(out["password"], "pw")
        self.assertNotIn("tls", out)


class RejectionTests(unittest.TestCase):
    def test_not_json_falls_through_to_link(self):
        link = "vless://11111111-2222-3333-4444-555555555555@1.2.3.4:443?type=tcp#x"
        out = B.parse_link(link)
        self.assertEqual(out["type"], "vless")

    def test_broken_json(self):
        self.assertIsNone(B.parse_link("{не json"))

    def test_config_without_usable_outbound(self):
        cfg = {"outbounds": [{"protocol": "freedom"}, {"protocol": "blackhole"}]}
        self.assertIsNone(B.parse_link(json.dumps(cfg)))

    def test_vless_without_uuid(self):
        cfg = {"outbounds": [{"protocol": "vless", "settings": {
            "vnext": [{"address": "a", "port": 443, "users": [{}]}]}}]}
        self.assertIsNone(B.parse_link(json.dumps(cfg)))

    def test_reality_without_public_key(self):
        cfg = json.loads(json.dumps(VLESS_REALITY))
        del cfg["outbounds"][0]["streamSettings"]["realitySettings"]["publicKey"]
        self.assertIsNone(B.parse_link(json.dumps(cfg)))

    def test_ready_singbox_outbound_passed_through(self):
        out = {"type": "vless", "server": "a", "server_port": 443, "uuid": "u"}
        self.assertEqual(B.parse_link(json.dumps(out))["type"], "vless")

    def test_empty(self):
        self.assertIsNone(B.parse_link(""))


class RawNotTruncatedTests(unittest.TestCase):
    """Узел хранил весь конфиг, обрезанный до 2000 символов.

    Обрезка рвала JSON посередине строки: мост получал невалидный
    документ, молча не поднимался, и узел висел «живым, но неприменимым».
    """

    def test_big_config_round_trips(self):
        from logic.vpn_sub import _parse_xray_config

        cfg = json.loads(json.dumps(VLESS_REALITY))
        # Надуваем конфиг: настоящие подписки приходят на 3-5 КБ.
        cfg["routing"]["rules"] = [
            {"domain": ["domain:example%d.com" % i], "outboundTag": "direct"}
            for i in range(200)
        ]
        self.assertGreater(len(json.dumps(cfg)), 2000)

        nodes = _parse_xray_config(cfg, "test")
        self.assertEqual(len(nodes), 1)
        raw = nodes[0].raw
        self.assertGreater(len(raw), 0)
        # Главное: raw — валидный JSON, а не обрезанный огрызок.
        parsed = json.loads(raw)
        self.assertIn("outbounds", parsed)
        self.assertIsNotNone(B.parse_link(raw), "raw не разбирается мостом")

    def test_raw_has_no_giant_routing_rules(self):
        """В узле лежит только outbound, без всего конфига."""
        from logic.vpn_sub import _parse_xray_config

        cfg = json.loads(json.dumps(VLESS_REALITY))
        nodes = _parse_xray_config(cfg, "test")
        self.assertNotIn("routing", nodes[0].raw)
        self.assertNotIn("inbounds", nodes[0].raw)


class BridgeConfigTests(unittest.TestCase):
    def test_build_config_wraps_outbound(self):
        out = B.parse_link(json.dumps(VLESS_REALITY))
        cfg = B.build_config("key", out, 20001)
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 20001)
        self.assertEqual(cfg["inbounds"][0]["type"], "socks")
        self.assertEqual(cfg["outbounds"][0]["tag"], "out")
        self.assertEqual(cfg["route"]["final"], "out")

    def test_config_is_valid_json_serialisable(self):
        out = B.parse_link(json.dumps(VLESS_REALITY))
        json.dumps(B.build_config("k", out, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
