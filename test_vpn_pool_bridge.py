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
