"""Прокси в менеджере ботов: разрешение ключей, раздача, проверка до захода.

Сети нет. Пул подменяется фейком с заранее известными ответами.
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

import bot as botmod
from logic import vpn_sub as VS
from logic.proxy_pool import ProxyPool


SOCKS = "socks5://user:pass@127.0.0.1:10808"
VLESS = ("vless://uuid@de.example.org:443?security=reality"
         "&sni=de.example.org#Германия")


class FakePool:
    """Пул с предсказуемыми ответами: что живое, что мёртвое."""

    def __init__(self, usable=(), nodes=None):
        self._usable = set(usable)
        self._nodes = list(nodes or [])
        self.verified = []

    def find_node(self, needle):
        needle = (needle or "").lower()
        for node in self._nodes:
            if needle in (node.name.lower(), node.address.lower(), node.key.lower()):
                return node
        return None

    def verify_before_connect(self, proxy, max_age=20.0):
        self.verified.append(proxy)
        if proxy in self._usable:
            return True, "прокси живой (12мс)"
        return False, "socks5: TimeoutError"

    def usable_proxies(self):
        return sorted(self._usable)

    def statuses(self):
        return []


def make_manager(pool):
    """BotManager без файлов и без сети."""
    manager = botmod.BotManager.__new__(botmod.BotManager)
    manager.bots = {}
    manager._next_id = 1
    manager.active_bot_id = None
    manager._lock = threading.RLock()
    manager.proxy_pool = pool
    manager._nick_index = {}
    manager._obj_index = {}
    manager.connect_gate = MagicMock()
    manager.saved = 0
    manager.save_accounts = lambda *a, **k: setattr(manager, "saved", manager.saved + 1)
    manager.list_bots = lambda *a, **k: None
    return manager


def add_fake_bot(manager, nick, proxy=""):
    bot = MagicMock()
    bot.bot_id = manager._next_id
    bot.nickname = nick
    bot.proxy = proxy
    bot.is_banned = False
    manager.bots[bot.bot_id] = bot
    manager._next_id += 1
    return bot


class ResolveProxyTests(unittest.TestCase):
    def setUp(self):
        self.node = VS.parse_link(SOCKS)
        self.node.name = "HAPP"
        self.pool = FakePool(usable=[SOCKS], nodes=[self.node])
        self.manager = make_manager(self.pool)

    def test_direct_url_passes_through(self):
        self.assertEqual(self.manager.resolve_proxy(SOCKS), SOCKS)

    def test_http_rewritten_to_socks5(self):
        self.assertEqual(
            self.manager.resolve_proxy("http://1.2.3.4:8080"),
            "socks5://1.2.3.4:8080",
        )

    def test_pool_name_resolves(self):
        self.assertEqual(self.manager.resolve_proxy("HAPP"), SOCKS)

    def test_none_clears(self):
        for value in ("none", "off", "direct", "-", "0", "no", ""):
            self.assertEqual(self.manager.resolve_proxy(value), "")

    def test_vless_node_rejected(self):
        vless_node = VS.parse_link(VLESS)
        vless_node.name = "DE"
        self.pool._nodes.append(vless_node)
        self.assertEqual(self.manager.resolve_proxy("DE"), "")

    def test_unknown_key_returned_as_is(self):
        self.assertEqual(self.manager.resolve_proxy("чужое"), "чужое")


class SetBotProxyTests(unittest.TestCase):
    def setUp(self):
        node = VS.parse_link(SOCKS)
        node.name = "HAPP"
        self.pool = FakePool(usable=[SOCKS], nodes=[node])
        self.manager = make_manager(self.pool)
        self.bot = add_fake_bot(self.manager, "Test")

    def test_set_by_pool_name(self):
        self.assertTrue(self.manager.set_bot_proxy(self.bot.bot_id, "HAPP"))
        self.assertEqual(self.bot.proxy, SOCKS)

    def test_reset(self):
        self.bot.proxy = SOCKS
        self.assertTrue(self.manager.set_bot_proxy(self.bot.bot_id, "none"))
        self.assertEqual(self.bot.proxy, "")

    def test_unknown_bot(self):
        self.assertFalse(self.manager.set_bot_proxy(999, SOCKS))

    def test_vless_refused(self):
        vless_node = VS.parse_link(VLESS)
        vless_node.name = "DE"
        self.pool._nodes.append(vless_node)
        self.assertFalse(self.manager.set_bot_proxy(self.bot.bot_id, "DE"))
        self.assertEqual(self.bot.proxy, "")


class ApplyProxyTests(unittest.TestCase):
    def setUp(self):
        node = VS.parse_link(SOCKS)
        node.name = "HAPP"
        self.pool = FakePool(usable=[SOCKS], nodes=[node])
        self.manager = make_manager(self.pool)
        self.a = add_fake_bot(self.manager, "A")
        self.b = add_fake_bot(self.manager, "B")

    def test_apply_to_all(self):
        report = self.manager.apply_proxy_to_bots("HAPP")
        self.assertTrue(report["ok"])
        self.assertEqual(self.a.proxy, SOCKS)
        self.assertEqual(self.b.proxy, SOCKS)
        self.assertEqual(len(report["applied"]), 2)

    def test_apply_to_selected(self):
        report = self.manager.apply_proxy_to_bots("HAPP", [self.a.bot_id])
        self.assertEqual(report["applied"], [self.a.bot_id])
        self.assertEqual(self.a.proxy, SOCKS)
        self.assertEqual(self.b.proxy, "")

    def test_dead_proxy_not_applied(self):
        report = self.manager.apply_proxy_to_bots("socks5://127.0.0.1:9")
        self.assertFalse(report["ok"])
        self.assertEqual(self.a.proxy, "")
        self.assertEqual(report["applied"], [])

    def test_verification_happens_before_apply(self):
        self.manager.apply_proxy_to_bots("HAPP")
        self.assertIn(SOCKS, self.pool.verified)


class PreflightTests(unittest.TestCase):
    def _bot(self, proxy, pool):
        bot = botmod.BotInstance.__new__(botmod.BotInstance)
        bot.proxy = proxy
        bot.manager = MagicMock()
        bot.manager.proxy_pool = pool
        bot.logs = []
        bot.add_log = lambda text: bot.logs.append(text)
        return bot

    def test_no_proxy_passes(self):
        bot = self._bot("", FakePool())
        ok, _reason = bot.check_proxy_before_start()
        self.assertTrue(ok)

    def test_live_proxy_passes(self):
        bot = self._bot(SOCKS, FakePool(usable=[SOCKS]))
        ok, reason = bot.check_proxy_before_start()
        self.assertTrue(ok, reason)
        self.assertTrue(any("пройдена" in line for line in bot.logs))

    def test_dead_proxy_blocks_start(self):
        bot = self._bot(SOCKS, FakePool())
        ok, reason = bot.check_proxy_before_start()
        self.assertFalse(ok)
        self.assertIn("TimeoutError", reason)
        self.assertTrue(any("НЕ пройдена" in line for line in bot.logs))

    def test_start_refused_when_proxy_dead(self):
        bot = self._bot(SOCKS, FakePool())
        bot.is_banned = False
        bot.status = ""
        started = botmod.BotInstance.start(bot)
        self.assertFalse(started)
        self.assertIn("Прокси не работает", bot.status)

    def test_no_pool_does_not_block(self):
        bot = self._bot(SOCKS, None)
        ok, _reason = bot.check_proxy_before_start()
        self.assertTrue(ok)


class ReportTests(unittest.TestCase):
    def test_report_without_pool(self):
        manager = make_manager(None)
        self.assertIn("не запущен", manager.proxy_report())

    def test_report_empty_pool(self):
        pool = ProxyPool(os.path.join(tempfile.mkdtemp(), "p.json"),
                         loader=lambda u, **k: [], log=lambda _m: None)
        manager = make_manager(pool)
        self.assertIn("Прокси нет", manager.proxy_report())


class BatchProxyTests(unittest.TestCase):
    def test_batch_skips_dead_proxies(self):
        node = VS.parse_link(SOCKS)
        node.name = "HAPP"
        pool = FakePool(usable=[SOCKS], nodes=[node])
        manager = make_manager(pool)
        created = []

        def fake_add(target_server, nick, password="", **kwargs):
            created.append(kwargs.get("proxy"))
            return add_fake_bot(manager, nick, kwargs.get("proxy", ""))

        manager.add_bot = fake_add
        manager.generate_random_nickname = lambda: "Nick"
        manager.add_bots_batch(
            "KALUGA", 2, password="x", proxies=["HAPP", "socks5://127.0.0.1:9"]
        )
        # мёртвый выкинут, живой роздан обоим
        self.assertEqual(created, [SOCKS, SOCKS])


if __name__ == "__main__":
    unittest.main(verbosity=2)
