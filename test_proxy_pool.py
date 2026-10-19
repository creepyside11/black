"""Подписки, узлы, проверка и пул прокси.

Сети нет: подписки подсовываются фейковым загрузчиком, проверка — фейковым
пробером. Проверяем логику, а не чужой сервер.
"""

import base64
import json
import os
import tempfile
import unittest

from logic import proxy_pool as PP
from logic import vpn_probe as VP
from logic import vpn_sub as VS


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


VLESS_LINK = (
    "vless://84d2af53-66b1-475a-801c-3acd0178b887@deu.example.org:443"
    "?encryption=none&type=tcp&security=reality&sni=deu.example.org#Германия"
)
SOCKS_LINK = "socks5://user:pass@127.0.0.1:10808#Локальный"
HTTP_LINK = "http://10.0.0.5:8080"
STUB_LINK = (
    "vless://00000000-0000-0000-0000-000000000000@0.0.0.0:1"
    "?encryption=none&type=tcp&security=none#App not supported or HWID"
)


class ParseLinkTests(unittest.TestCase):
    def test_vless(self):
        node = VS.parse_link(VLESS_LINK)
        self.assertEqual(node.protocol, "vless")
        self.assertEqual(node.address, "deu.example.org")
        self.assertEqual(node.port, 443)
        self.assertEqual(node.security, "reality")
        self.assertEqual(node.name, "Германия")
        self.assertTrue(node.needs_client)
        self.assertFalse(node.is_direct)

    def test_socks5_with_auth(self):
        node = VS.parse_link(SOCKS_LINK)
        self.assertEqual(node.protocol, "socks5")
        self.assertEqual(node.login, "user")
        self.assertEqual(node.password, "pass")
        self.assertTrue(node.is_direct)
        self.assertEqual(node.proxy_url(), "socks5://user:pass@127.0.0.1:10808")

    def test_http(self):
        node = VS.parse_link(HTTP_LINK)
        self.assertEqual(node.protocol, "http")
        self.assertEqual(node.address, "10.0.0.5")
        self.assertEqual(node.port, 8080)

    def test_stub_detected(self):
        node = VS.parse_link(STUB_LINK)
        self.assertTrue(node.stub)

    def test_stub_by_marker(self):
        link = "vless://uuid@real.example.org:443?security=tls#App not supported"
        self.assertTrue(VS.parse_link(link).stub)

    def test_garbage_returns_none(self):
        self.assertIsNone(VS.parse_link("не ссылка"))
        self.assertIsNone(VS.parse_link("ftp://x:1"))
        self.assertIsNone(VS.parse_link("vless://без-собаки"))

    def test_trojan(self):
        node = VS.parse_link("trojan://secret@tj.example.org:443?sni=tj.example.org#TJ")
        self.assertEqual(node.protocol, "trojan")
        self.assertEqual(node.password, "secret")
        self.assertEqual(node.security, "tls")

    def test_vmess(self):
        payload = json.dumps({"add": "vm.example.org", "port": 8443, "ps": "VM", "tls": "tls"})
        node = VS.parse_link("vmess://" + b64(payload))
        self.assertEqual(node.protocol, "vmess")
        self.assertEqual(node.port, 8443)

    def test_shadowsocks(self):
        creds = b64("aes-256-gcm:pass123")
        node = VS.parse_link(f"ss://{creds}@ss.example.org:8388#SS")
        self.assertEqual(node.protocol, "ss")
        self.assertEqual(node.login, "aes-256-gcm")
        self.assertEqual(node.password, "pass123")

    def test_ipv6_host(self):
        node = VS.parse_link("socks5://[2001:db8::1]:1080")
        self.assertEqual(node.address, "2001:db8::1")
        self.assertEqual(node.port, 1080)


class DecodeSubscriptionTests(unittest.TestCase):
    def test_base64_links(self):
        body = b64("\n".join([VLESS_LINK, SOCKS_LINK]))
        nodes = VS.decode_subscription(body)
        self.assertEqual(len(nodes), 2)
        self.assertEqual({n.protocol for n in nodes}, {"vless", "socks5"})

    def test_plain_links(self):
        nodes = VS.decode_subscription(f"{VLESS_LINK}\n{SOCKS_LINK}\n")
        self.assertEqual(len(nodes), 2)

    def test_xray_json(self):
        config = {
            "remarks": "DE",
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {"vnext": [{"address": "de.example.org", "port": 443,
                                            "users": [{"id": "uuid"}]}]},
                    "streamSettings": {"network": "tcp", "security": "reality",
                                       "realitySettings": {"serverName": "de.example.org"}},
                },
                {"protocol": "freedom"},
            ],
        }
        nodes = VS.decode_subscription(json.dumps([config]))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].address, "de.example.org")
        self.assertEqual(nodes[0].security, "reality")
        self.assertEqual(nodes[0].sni, "de.example.org")

    def test_xray_shadowsocks(self):
        config = {"remarks": "SS", "outbounds": [{
            "protocol": "shadowsocks",
            "settings": {"servers": [{"address": "ss.example.org", "port": 8388,
                                      "password": "pw"}]},
        }]}
        nodes = VS.decode_subscription(json.dumps(config))
        self.assertEqual(nodes[0].protocol, "ss")

    def test_stub_subscription_raises(self):
        with self.assertRaises(VS.SubscriptionError):
            VS.decode_subscription(b64(STUB_LINK))

    def test_empty_raises(self):
        with self.assertRaises(VS.SubscriptionError):
            VS.decode_subscription("")

    def test_dedupe(self):
        nodes = VS.decode_subscription(f"{VLESS_LINK}\n{VLESS_LINK}")
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(VS.dedupe(nodes)), 1)


class FakeProber:
    """Проверка без сети: узлы, отмеченные в плане, получают нужный статус."""

    def __init__(self, plan=None, default_alive=True):
        self.plan = plan or {}
        self.default_alive = default_alive
        self.calls = 0

    def __call__(self, nodes, **kwargs):
        self.calls += 1
        results = []
        for node in nodes:
            alive, usable = self.plan.get(node.key, (self.default_alive, False))
            result = VP.ProbeResult(node=node, alive=alive, usable=usable)
            if usable:
                result.status = VP.STATUS_OK
                result.latency_ms = 42.0
            elif alive:
                result.status = VP.STATUS_NEEDS_CLIENT
            else:
                result.status = VP.STATUS_DEAD
                result.reason = "мёртв"
            results.append(result)
        return results


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "pool.json")

    def _pool(self, plan=None, loader=None, default_alive=True, **kwargs):
        return PP.ProxyPool(
            self.path,
            loader=loader or (lambda url, **k: VS.decode_subscription(
                b64("\n".join([VLESS_LINK, SOCKS_LINK])))),
            prober=FakeProber(plan, default_alive=default_alive),
            log=lambda _m: None,
            **kwargs,
        )

    def test_add_subscription_fetches_nodes(self):
        pool = self._pool()
        nodes = pool.add_subscription("https://sub.example.org/abc")
        self.assertEqual(len(nodes), 2)
        self.assertEqual(len(pool.subscriptions), 1)

    def test_bad_subscription_rejected(self):
        def loader(url, **kwargs):
            raise VS.SubscriptionError("нет узлов")

        pool = self._pool(loader=loader)
        with self.assertRaises(PP.ProxyPoolError):
            pool.add_subscription("https://sub.example.org/bad")
        self.assertEqual(pool.subscriptions, [])

    def test_refresh_sets_statuses(self):
        pool = self._pool()
        pool.add_subscription("https://sub.example.org/abc")
        results = pool.refresh()
        self.assertEqual(len(results), 2)
        self.assertEqual(pool.summary()["total"], 2)

    def test_usable_only_working(self):
        plan = {VS.parse_link(SOCKS_LINK).key: (True, True),
                VS.parse_link(VLESS_LINK).key: (True, False)}
        pool = self._pool(plan=plan)
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        usable = pool.usable()
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0].node.protocol, "socks5")
        self.assertEqual(pool.usable_proxies(), ["socks5://user:pass@127.0.0.1:10808"])

    def test_dead_marked(self):
        plan = {VS.parse_link(SOCKS_LINK).key: (False, False),
                VS.parse_link(VLESS_LINK).key: (False, False)}
        pool = self._pool(plan=plan)
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        self.assertEqual(pool.summary()["dead"], 2)
        self.assertEqual(pool.usable_proxies(), [])

    def test_sorted_working_first(self):
        plan = {VS.parse_link(SOCKS_LINK).key: (True, True),
                VS.parse_link(VLESS_LINK).key: (False, False)}
        pool = self._pool(plan=plan)
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        self.assertTrue(pool.statuses()[0].usable)

    def test_best_is_working(self):
        plan = {VS.parse_link(SOCKS_LINK).key: (True, True),
                VS.parse_link(VLESS_LINK).key: (True, False)}
        pool = self._pool(plan=plan)
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        self.assertEqual(pool.best().node.protocol, "socks5")

    def test_best_none_when_all_dead(self):
        pool = self._pool(plan={}, default_alive=False)
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        self.assertIsNone(pool.best())

    def test_find_by_name_and_address(self):
        pool = self._pool()
        pool.add_subscription("https://sub.example.org/abc")
        pool.refresh()
        self.assertIsNotNone(pool.find("Германия"))
        self.assertIsNotNone(pool.find("deu.example.org"))
        self.assertIsNone(pool.find("ничего такого"))

    def test_persist_and_reload(self):
        pool = self._pool()
        pool.add_subscription("https://sub.example.org/abc", name="Моя")
        again = PP.ProxyPool(self.path, prober=FakeProber(), log=lambda _m: None)
        self.assertEqual(len(again.subscriptions), 1)
        self.assertEqual(again.subscriptions[0]["name"], "Моя")

    def test_remove_subscription(self):
        pool = self._pool()
        pool.add_subscription("https://sub.example.org/abc", name="Моя")
        self.assertTrue(pool.remove_subscription("Моя"))
        self.assertEqual(pool.subscriptions, [])

    def test_manual_proxy_added_and_usable(self):
        plan = {"socks5://127.0.0.1:10808": (True, True)}
        pool = PP.ProxyPool(
            self.path,
            loader=lambda url, **k: [],
            prober=FakeProber(plan),
            log=lambda _m: None,
        )
        node = pool.add_manual("socks5://127.0.0.1:10808", name="HAPP")
        self.assertEqual(node.protocol, "socks5")
        pool.refresh(fetch=False)
        self.assertEqual(pool.usable_proxies(), ["socks5://127.0.0.1:10808"])

    def test_manual_accepts_vless_as_bridge_node(self):
        """Ключ vless принимается: его поднимает мост sing-box.

        Раньше он отклонялся с «бот умеет только socks5», и добавить узел
        из подписки вручную было нельзя вовсе.
        """
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        node = pool.add_manual(VLESS_LINK, name="DE")
        self.assertEqual(node.protocol, "vless")
        self.assertEqual(node.name, "DE")
        self.assertEqual([n.protocol for n in pool.nodes()], ["vless"])

    def test_manual_vless_survives_reload(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        pool.add_manual(VLESS_LINK, name="DE")
        again = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        names = [n.name for n in again.nodes()]
        self.assertIn("DE", names)

    def test_manual_survives_reload(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        pool.add_manual("socks5://127.0.0.1:10808", name="HAPP")
        again = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        self.assertEqual(len(again.manual), 1)
        self.assertEqual(again.manual[0]["name"], "HAPP")

    def test_empty_pool_refresh_is_safe(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        self.assertEqual(pool.refresh(), [])
        self.assertIsNone(pool.best())

    def test_verify_before_connect_ok(self):
        plan = {"socks5://127.0.0.1:10808": (True, True)}
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [],
                            prober=FakeProber(plan), log=lambda _m: None)
        ok, reason = pool.verify_before_connect("socks5://127.0.0.1:10808")
        self.assertTrue(ok, reason)

    def test_verify_before_connect_dead(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [],
                            prober=FakeProber({}, default_alive=False),
                            log=lambda _m: None)
        ok, reason = pool.verify_before_connect("socks5://127.0.0.1:10808")
        self.assertFalse(ok)
        self.assertIn("мёртв", reason)

    def test_verify_before_connect_rejects_vless(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        ok, reason = pool.verify_before_connect(VLESS_LINK)
        self.assertFalse(ok)
        self.assertIn("не поддерживается", reason)

    def test_verify_before_connect_rejects_garbage(self):
        pool = PP.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        ok, _reason = pool.verify_before_connect("чепуха")
        self.assertFalse(ok)


class TimerTests(unittest.TestCase):
    def test_start_and_stop(self):
        path = os.path.join(tempfile.mkdtemp(), "p.json")
        prober = FakeProber()
        pool = PP.ProxyPool(path, loader=lambda u, **k: [VS.parse_link(SOCKS_LINK)],
                            prober=prober, log=lambda _m: None, interval=0.05)
        pool.add_subscription("https://sub.example.org/abc")
        pool.start()
        import time
        time.sleep(0.3)
        pool.stop()
        self.assertFalse(pool.running)
        self.assertGreaterEqual(prober.calls, 1)

    def test_stop_without_start_is_safe(self):
        path = os.path.join(tempfile.mkdtemp(), "p.json")
        pool = PP.ProxyPool(path, loader=lambda u, **k: [], log=lambda _m: None)
        pool.stop()


class ProbeLogicTests(unittest.TestCase):
    """probe_node без сети: заглушки и мусор отсекаются до коннекта."""

    def test_stub_is_dead(self):
        node = VS.parse_link(STUB_LINK)
        result = VP.probe_node(node, timeout=0.1)
        self.assertFalse(result.alive)
        self.assertEqual(result.status, VP.STATUS_DEAD)
        self.assertIn("заглушка", result.reason)

    def test_missing_port_is_dead(self):
        node = VS.VpnNode(name="x", protocol="socks5", address="1.2.3.4", port=0)
        result = VP.probe_node(node, timeout=0.1)
        self.assertFalse(result.alive)

    def test_unreachable_socks5_is_dead(self):
        node = VS.VpnNode(name="x", protocol="socks5", address="127.0.0.1", port=9)
        result = VP.probe_node(node, timeout=0.3, check_udp=False)
        self.assertFalse(result.alive)
        self.assertEqual(result.status, VP.STATUS_DEAD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
