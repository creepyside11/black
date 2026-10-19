"""Пул с мостом: приём ключей, раздача, ротация.

Закрывает две жалобы:
  * новый ключ vless:// не добавлялся — уходил в загрузчик подписок;
  * узлы из подписки висели неприменяемыми, применить их было некуда.
"""

import os
import tempfile
import unittest
from unittest import mock

from logic import proxy_pool as P
from logic.vpn_probe import ProbeResult, STATUS_DEAD, STATUS_NEEDS_CLIENT, STATUS_OK
from logic.vpn_sub import VpnNode

VLESS = ("vless://11111111-2222-3333-4444-555555555555@deu.example.com:443"
         "?type=tcp&security=reality&pbk=PK&sid=ab&sni=www.microsoft.com#Germany")
SOCKS = "socks5://127.0.0.1:10808"


class FakeBridge:
    def __init__(self, *, up=True):
        self.up = up
        self.started = []
        self._ports = {}

    def available(self):
        return True

    def start(self, key, link):
        self.started.append((key, link))
        if not self.up:
            return None
        port = P.PORT_FOR_TEST + len(self._ports)
        self._ports[key] = port
        return f"socks5://127.0.0.1:{port}"

    def port_of(self, key):
        return self._ports.get(key)

    def stop_all(self):
        pass


P.PORT_FOR_TEST = 21000


def node(proto="vless", host="deu.example.com", port=443, name="Germany", raw=VLESS):
    return VpnNode(name=name, protocol=proto, address=host, port=port, raw=raw)


class AddAnyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.pool = P.ProxyPool(path=self._tmp.name, bridge=FakeBridge())

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_single_vless_key_accepted(self):
        nodes = self.pool.add_any(VLESS, name="")
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].protocol, "vless")

    def test_key_with_name_prefix(self):
        nodes = self.pool.add_any(VLESS, name="Германия")
        self.assertEqual(nodes[0].name, "Германия")

    def test_several_keys_one_per_line(self):
        second = VLESS.replace("deu.example.com", "usa.example.com").replace("#Germany", "#USA")
        nodes = self.pool.add_any(f"{VLESS}\n{second}")
        self.assertEqual(len(nodes), 2)

    def test_garbage_rejected_with_clear_message(self):
        with self.assertRaises(P.ProxyPoolError) as ctx:
            self.pool.add_any("просто текст")
        self.assertIn("vless://", str(ctx.exception))

    def test_empty_rejected(self):
        with self.assertRaises(P.ProxyPoolError):
            self.pool.add_any("   ")

    def test_url_still_goes_to_subscription(self):
        with mock.patch.object(self.pool, "add_subscription", return_value=[]) as sub:
            self.pool.add_any("https://panel.example/sub/abc")
        sub.assert_called_once()


class BridgeProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.bridge = FakeBridge()
        self.pool = P.ProxyPool(path=self._tmp.name, bridge=self.bridge)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_client_node_goes_through_bridge_and_becomes_usable(self):
        target = node()

        def prober(nodes, **kwargs):
            n = nodes[0]
            if n.protocol == "vless":
                return [ProbeResult(node=n, status=STATUS_NEEDS_CLIENT, alive=True)]
            return [ProbeResult(node=n, status=STATUS_OK, alive=True, usable=True,
                                latency_ms=42.0, reason="ok")]

        self.pool._prober = prober
        result = self.pool.check_node(target)

        self.assertTrue(result.usable, "узел через мост должен стать рабочим")
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.socks_url.startswith("socks5://127.0.0.1:"))
        self.assertEqual(result.proxy_url, result.socks_url)
        self.assertEqual(len(self.bridge.started), 1)

    def test_bridge_failure_keeps_node_unusable(self):
        self.bridge.up = False
        target = node()

        def prober(nodes, **kwargs):
            n = nodes[0]
            return [ProbeResult(node=n, status=STATUS_NEEDS_CLIENT, alive=True)]

        self.pool._prober = prober
        result = self.pool.check_node(target)
        self.assertFalse(result.usable)
        self.assertIn("мост", result.reason)

    def test_bridge_up_but_traffic_dead_is_dead_not_needs_client(self):
        target = node()

        def prober(nodes, **kwargs):
            n = nodes[0]
            if n.protocol == "vless":
                return [ProbeResult(node=n, status=STATUS_NEEDS_CLIENT, alive=True)]
            return [ProbeResult(node=n, status=STATUS_DEAD, alive=False, reason="не пускает")]

        self.pool._prober = prober
        result = self.pool.check_node(target)
        self.assertFalse(result.usable)
        self.assertEqual(result.status, STATUS_DEAD)
        self.assertIn("не пускает", result.reason)

    def test_no_bridge_leaves_needs_client(self):
        self.pool.bridge = None
        target = node()

        def prober(nodes, **kwargs):
            n = nodes[0]
            return [ProbeResult(node=n, status=STATUS_NEEDS_CLIENT, alive=True)]

        self.pool._prober = prober
        result = self.pool.check_node(target)
        self.assertEqual(result.status, STATUS_NEEDS_CLIENT)


class SocksUrlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.bridge = FakeBridge()
        self.pool = P.ProxyPool(path=self._tmp.name, bridge=self.bridge)

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_direct_node_returns_itself(self):
        direct = VpnNode(name="local", protocol="socks5", address="127.0.0.1", port=10808)
        self.assertEqual(self.pool.socks_url_for(direct), SOCKS)

    def test_client_node_returns_bridge_socks(self):
        url = self.pool.socks_url_for(node())
        self.assertTrue(url.startswith("socks5://127.0.0.1:"))

    def test_no_bridge_returns_empty(self):
        self.pool.bridge = None
        self.assertEqual(self.pool.socks_url_for(node()), "")


class RotateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.pool = P.ProxyPool(path=self._tmp.name, bridge=FakeBridge())

    def tearDown(self):
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _fake_usable(self, count):
        results = []
        for i in range(count):
            n = node(host=f"n{i}.example.com", name=f"node{i}", raw=VLESS)
            results.append(ProbeResult(node=n, status=STATUS_OK, alive=True,
                                       usable=True, socks_url=f"socks5://127.0.0.1:{22000+i}"))
        self.pool.usable = lambda: results

    def test_spreads_bots_across_nodes(self):
        self._fake_usable(3)
        plan = self.pool.rotate(9, per_node=3)
        self.assertEqual(len(plan), 9)
        self.assertEqual(len(set(plan)), 3, "все боты сели на один узел")

    def test_per_node_limit_respected(self):
        self._fake_usable(2)
        plan = self.pool.rotate(4, per_node=2)
        self.assertEqual(len(set(plan)), 2)

    def test_no_usable_returns_empty_strings(self):
        self.pool.usable = lambda: []
        self.assertEqual(self.pool.rotate(5), [""] * 5)

    def test_zero_bots(self):
        self.assertEqual(self.pool.rotate(0), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NodeKeyTests(unittest.TestCase):
    """Ключ узла должен различать аккаунты на одном сервере.

    В подписке несколько узлов висят на одном хосте и порту с разными
    UUID. Ключ был «протокол://адрес:порт», они сливались: общий порт
    моста, общий туннель, и боту отдавался чужой узел.
    """

    def _node(self, login, name="DE"):
        return VpnNode(
            name=name, protocol="vless", address="site.babay.su", port=443,
            security="reality", login=login,
        )

    def test_same_host_different_uuid_differ(self):
        a = self._node("11111111-1111-1111-1111-111111111111")
        b = self._node("22222222-2222-2222-2222-222222222222")
        self.assertNotEqual(a.key, b.key)

    def test_same_node_same_key(self):
        a = self._node("11111111-1111-1111-1111-111111111111")
        b = self._node("11111111-1111-1111-1111-111111111111")
        self.assertEqual(a.key, b.key)

    def test_key_starts_with_scheme(self):
        node = self._node("abc")
        self.assertTrue(node.key.startswith("vless://site.babay.su:443"))

    def test_direct_node_key_stays_readable(self):
        """По ключу ищут узел и сверяют статусы — отпечаток там мешает."""
        node = VpnNode(name="HAPP", protocol="socks5", address="127.0.0.1",
                       port=10808, raw="socks5://127.0.0.1:10808")
        self.assertEqual(node.key, "socks5://127.0.0.1:10808")

    def test_node_without_login_still_has_key(self):
        node = VpnNode(name="x", protocol="vless", address="a.b", port=443)
        self.assertEqual(node.key, "vless://a.b:443")

    def test_same_uuid_different_config_differ(self):
        """Подписка выдаёт всем узлам один UUID, различает их только конфиг."""
        a = VpnNode(name="DE", protocol="vless", address="site.babay.su",
                    port=443, security="reality", login="same-uuid",
                    raw='{"outbounds":[{"server":"site.babay.su","sni":"a"}]}')
        b = VpnNode(name="Cheburnet", protocol="vless", address="site.babay.su",
                    port=443, security="reality", login="same-uuid",
                    raw='{"outbounds":[{"server":"site.babay.su","sni":"b"}]}')
        self.assertNotEqual(a.key, b.key)

    def test_identical_configs_share_key(self):
        raw = '{"outbounds":[{"server":"x"}]}'
        a = VpnNode(name="A", protocol="vless", address="x", port=443, raw=raw)
        b = VpnNode(name="B", protocol="vless", address="x", port=443, raw=raw)
        self.assertEqual(a.key, b.key)

    def test_find_node_still_matches_by_address(self):
        """Поиск по адресу не должен сломаться из-за UUID в ключе."""
        node = self._node("11111111-1111-1111-1111-111111111111")
        self.assertIn("site.babay.su", node.key)


class RefreshBridgeTests(unittest.TestCase):
    """Плановая проверка обязана доводить узлы до моста.

    refresh() гонял пробник одной пачкой и check_node не вызывал — весь
    путь через мост существовал только на бумаге. С установленным
    sing-box подписка всё равно оставалась жёлтой.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.path = self._tmp.name

    def tearDown(self):
        import os
        for suffix in ("", ".bak", ".tmp"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def _pool(self, nodes, bridge):
        pool = P.ProxyPool(self.path, loader=lambda u, **k: [], log=lambda _m: None)
        pool.bridge = bridge
        # nodes() собирает из кэша подписок, а не из поля _nodes.
        pool._cached_nodes = list(nodes)
        return pool

    def _node(self, name, uuid):
        return VpnNode(name=name, protocol="vless", address="%s.example.com" % name,
                       port=443, security="reality", login=uuid,
                       raw='{"outbounds":[{"server":"%s"}]}' % name)

    def test_needs_client_nodes_go_through_bridge(self):
        node = self._node("de", "u1")

        class Bridge:
            def available(self):
                return True

            def start(self, key, link):
                return "socks5://127.0.0.1:20001"

            def port_of(self, key):
                return 20001

        pool = self._pool([node], Bridge())
        # Пробник: до моста узел «живой без клиента», после — рабочий.
        def prober(nodes, **kwargs):
            out = []
            for n in nodes:
                r = ProbeResult(node=n, alive=True, usable=n.port == 20001)
                r.status = STATUS_OK if r.usable else STATUS_NEEDS_CLIENT
                out.append(r)
            return out

        pool._prober = prober
        results = pool.refresh(fetch=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, STATUS_OK)
        self.assertEqual(results[0].socks_url, "socks5://127.0.0.1:20001")

    def test_no_bridge_leaves_them_needs_client(self):
        node = self._node("de", "u1")
        pool = self._pool([node], None)
        pool._prober = lambda nodes, **k: [
            ProbeResult(node=n, alive=True, usable=False, status=STATUS_NEEDS_CLIENT)
            for n in nodes
        ]
        results = pool.refresh(fetch=False)
        self.assertEqual(results[0].status, STATUS_NEEDS_CLIENT)

    def test_dead_nodes_are_not_probed_via_bridge(self):
        node = self._node("de", "u1")
        started = []

        class Bridge:
            def available(self):
                return True

            def start(self, key, link):
                started.append(key)
                return "socks5://127.0.0.1:20002"

            def port_of(self, key):
                return 20002

        pool = self._pool([node], Bridge())
        pool._prober = lambda nodes, **k: [
            ProbeResult(node=n, alive=False, usable=False, status=STATUS_DEAD)
            for n in nodes
        ]
        pool.refresh(fetch=False)
        self.assertEqual(started, [], "мёртвый узел незачем поднимать мостом")
