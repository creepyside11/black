"""Бан по IP: адрес в мусор, аккаунты с него — тоже.

Смысл: бан приходит на адрес, а не на аккаунт. Если адрес не помечен,
пул выдаст его следующему боту, и тот словит то же самое. Проверяем,
что забаненный адрес не попадает ни в раздачу, ни в проверку.
"""
import json
import os
import tempfile
import threading
import time
import unittest

from logic.proxy_pool import ProxyPool
from logic.vpn_probe import ProbeResult, STATUS_BANNED, STATUS_OK
from logic.vpn_sub import VpnNode


def _node(address="1.2.3.4", port=1080, name=None):
    return VpnNode(
        name=name or f"{address}:{port}",
        protocol="socks5",
        address=address,
        port=port,
        source="manual",
    )


def _ok(node):
    return ProbeResult(node=node, alive=True, usable=True, status=STATUS_OK,
                       latency_ms=100.0, checked_at=time.time())


class BannedProxyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "pool.json")
        self.nodes = [_node("1.2.3.4"), _node("5.6.7.8")]
        self.pool = ProxyPool(
            self.path,
            prober=lambda nodes, **kw: [_ok(n) for n in nodes],
            clock=lambda: 1000.0,
        )
        for node in self.nodes:
            self.pool.results[node.key] = _ok(node)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_banned_dropped_from_usable(self):
        self.assertEqual(len(self.pool.usable()), 2)
        self.pool.mark_banned(self.nodes[0].key, reason="connection banned")
        usable = self.pool.usable()
        self.assertEqual(len(usable), 1)
        self.assertEqual(usable[0].node.key, self.nodes[1].key)

    def test_banned_not_given_to_bots(self):
        self.pool.mark_banned(self.nodes[0].key)
        urls = self.pool.usable_proxies()
        self.assertNotIn("socks5://1.2.3.4:1080", urls)

    def test_banned_marked_in_statuses(self):
        self.pool.mark_banned(self.nodes[0].key, reason="бан по IP")
        statuses = self.pool.statuses()
        banned = [r for r in statuses if r.status == STATUS_BANNED]
        self.assertEqual(len(banned), 1)
        self.assertEqual(banned[0].node.key, self.nodes[0].key)
        self.assertIn("бан по IP", banned[0].reason)
        self.assertFalse(banned[0].usable)
        # Забаненный уезжает в конец списка.
        self.assertEqual(statuses[-1].node.key, self.nodes[0].key)

    def test_verify_refuses_banned(self):
        self.pool.mark_banned(self.nodes[0].key, reason="connection banned")
        ok, why = self.pool.verify_before_connect("socks5://1.2.3.4:1080")
        self.assertFalse(ok)
        self.assertIn("забанен", why)

    def test_ban_survives_restart(self):
        self.pool.mark_banned(self.nodes[0].key, reason="connection banned")
        again = ProxyPool(self.path, prober=lambda n, **k: [], clock=lambda: 1.0)
        self.assertTrue(again.is_banned("socks5://1.2.3.4:1080"))

    def test_unban_returns_to_pool(self):
        self.pool.mark_banned(self.nodes[0].key)
        self.assertEqual(len(self.pool.usable()), 1)
        self.assertTrue(self.pool.unban("socks5://1.2.3.4:1080"))
        self.assertEqual(len(self.pool.usable()), 2)

    def test_summary_counts_banned(self):
        self.pool.mark_banned(self.nodes[0].key)
        summary = self.pool.summary()
        self.assertEqual(summary["banned"], 1)
        self.assertEqual(summary["usable"], 1)
        self.assertEqual(summary["total"], 2)

    def test_rotate_never_uses_banned(self):
        self.pool.mark_banned(self.nodes[0].key)
        plan = self.pool.rotate(4)
        self.assertTrue(plan)
        self.assertTrue(all("1.2.3.4" not in url for url in plan))


class IpBanHandlerTests(unittest.TestCase):
    """handle_ip_ban: адрес помечен, все аккаунты с него — бан."""

    def setUp(self):
        import bot as botmod
        self.botmod = botmod
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "pool.json")
        self.pool = ProxyPool(self.path, prober=lambda n, **k: [],
                              clock=lambda: 1000.0)
        node = _node("9.9.9.9")
        self.pool.results[node.key] = _ok(node)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _bot(self, bid, proxy, nickname):
        class B:
            def __init__(self):
                self.bot_id = bid
                self.proxy = proxy
                self.nickname = nickname
                self.is_banned = False
                self.ban_reason = ""
                self.ban_kind = ""
                self.banned_at = 0.0
                self.status = ""
                self.logs = []

            def add_log(self, text):
                self.logs.append(text)

            def cancel_reconnect(self):
                pass

            def stop(self, banned=False):
                self.stopped = banned

        return B()

    def test_all_accounts_on_proxy_marked(self):
        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        b = self._bot(2, "socks5://9.9.9.9:1080", "B")
        c = self._bot(3, "socks5://8.8.8.8:1080", "C")

        pool = self.pool

        class Mgr:
            def __init__(self):
                self.bots = {1: a, 2: b, 3: c}
                self.proxy_pool = pool
                self.tg_controller = None
                self.saved = 0

            def save_accounts(self):
                self.saved += 1

        mgr = Mgr()
        out = self.botmod.BotManager.handle_ip_ban(mgr, a, reason="connection banned")

        self.assertTrue(a.is_banned)
        self.assertTrue(b.is_banned, "второй аккаунт с того же адреса тоже бан")
        self.assertFalse(c.is_banned, "аккаунт на другом адресе не трогаем")
        self.assertEqual(sorted(out["accounts"]), [1, 2])
        self.assertTrue(self.pool.is_banned("socks5://9.9.9.9:1080"))
        self.assertEqual(mgr.saved, 1)

    def test_no_proxy_does_not_crash(self):
        a = self._bot(1, "", "A")

        class Mgr:
            bots = {1: a}
            proxy_pool = None
            tg_controller = None

            def save_accounts(self):
                pass

        out = self.botmod.BotManager.handle_ip_ban(Mgr(), a, reason="x")
        self.assertEqual(out["accounts"], [])


class VpnPaginationTests(unittest.TestCase):
    """Узлы отдаются страницами: раньше пул рисовался одним куском."""

    def _bridge(self, pool):
        from logic import web_bridge

        class Mgr:
            proxy_pool = pool

        bridge = web_bridge.WebBridge.__new__(web_bridge.WebBridge)
        bridge.manager = Mgr()
        bridge.log = lambda *a, **k: None
        return bridge

    def test_pages_split(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            for i in range(50):
                node = _node(f"10.0.0.{i}")
                pool.results[node.key] = _ok(node)
            bridge = self._bridge(pool)

            first = bridge._vpn_payload(page=1, per_page=24)
            self.assertEqual(first["total"], 50)
            self.assertEqual(first["pages"], 3)
            self.assertEqual(len(first["nodes"]), 24)
            self.assertEqual(first["page"], 1)

            last = bridge._vpn_payload(page=3, per_page=24)
            self.assertEqual(len(last["nodes"]), 2, "50 = 24 + 24 + 2")

            # Номера страниц за пределами зажимаются.
            self.assertEqual(bridge._vpn_payload(page=99)["page"], 3)
            self.assertEqual(bridge._vpn_payload(page=0)["page"], 1)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_search_finds_node_on_other_page(self):
        """Поиск идёт по всему пулу, а не по открытой странице.

        Раньше фильтр применялся после нарезки: искал бы только внутри
        24 карточек и «не находил» узел, который лежит на третьей.
        """
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            for i in range(50):
                node = _node(f"10.0.0.{i}", name=f"узел-{i}")
                pool.results[node.key] = _ok(node)
            bridge = self._bridge(pool)

            found = bridge._vpn_payload(page=1, per_page=12, search="узел-47")
            self.assertEqual(found["total"], 1)
            self.assertEqual(found["pages"], 1)
            self.assertIn("узел-47", found["nodes"][0]["name"])

            by_addr = bridge._vpn_payload(search="10.0.0.33")
            self.assertEqual(by_addr["total"], 1)

            by_proto = bridge._vpn_payload(search="socks5")
            self.assertEqual(by_proto["total"], 50)

            self.assertEqual(bridge._vpn_payload(search="нет-такого")["total"], 0)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_only_usable_filter(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            good = _node("1.1.1.1")
            bad = _node("2.2.2.2")
            pool.results[good.key] = _ok(good)
            pool.results[bad.key] = ProbeResult(node=bad, alive=False,
                                                usable=False, status="dead")
            bridge = self._bridge(pool)
            self.assertEqual(bridge._vpn_payload(only_usable=True)["total"], 1)
            self.assertEqual(bridge._vpn_payload()["total"], 2)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_banned_reason_reaches_panel(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            node = _node("7.7.7.7")
            pool.results[node.key] = _ok(node)
            pool.mark_banned(node.key, reason="connection banned")
            bridge = self._bridge(pool)
            payload = bridge._vpn_payload()
            self.assertEqual(payload["nodes"][0]["status"], STATUS_BANNED)
            self.assertIn("connection banned", payload["nodes"][0]["reason"])
            self.assertEqual(payload["summary"]["banned"], 1)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
