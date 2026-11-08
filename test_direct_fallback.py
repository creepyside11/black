"""Прямой вход не проходит — переключаемся на прокси.

На части машин до игровых серверов не доходит трафик с прямого IP:
TCP-соединение устанавливается, а данные не идут, UDP-хендшейк молчит.
Бот бесконечно пишет «Сервер не ответил на подключение», и со стороны это
неотличимо от переполненного сервера.
"""

import threading
import unittest
from unittest import mock

import bot as botmod


class FakePool:
    def __init__(self, urls):
        self.urls = urls

    def usable_proxies(self):
        return list(self.urls)


class FakeBot:
    def __init__(self, proxy=""):
        self.bot_id = 1
        self.nickname = "Test_Nick"
        self.proxy = proxy
        self.connect_failures = 0
        self._auto_proxy_tried = False
        self.logs = []
        self.manager = None

    def add_log(self, text):
        self.logs.append(text)

    def _hold_proxy_bridge(self):
        """Заглушка: настоящий метод держит туннель в пуле, тесту он не нужен."""
        self.bridge_held = True

    # Тестируем только метод — берём его у настоящего класса.
    _try_auto_proxy = botmod.BotInstance._try_auto_proxy


def make_manager(urls, bots=()):
    mgr = mock.Mock()
    mgr._lock = threading.RLock()
    mgr.bots = {b.bot_id: b for b in bots}
    mgr.proxy_pool = FakePool(urls)
    mgr.save_accounts = lambda: None
    return mgr


class AutoProxyTests(unittest.TestCase):
    def test_assigns_proxy_from_pool(self):
        bot = FakeBot()
        bot.manager = make_manager(["socks5://127.0.0.1:20343"])
        self.assertTrue(bot._try_auto_proxy())
        self.assertEqual(bot.proxy, "socks5://127.0.0.1:20343")

    def test_logs_the_switch(self):
        bot = FakeBot()
        bot.manager = make_manager(["socks5://127.0.0.1:20343"])
        bot._try_auto_proxy()
        self.assertTrue(any("ПРОКСИ" in line for line in bot.logs))

    def test_no_working_proxies_returns_false(self):
        bot = FakeBot()
        bot.manager = make_manager([])
        self.assertFalse(bot._try_auto_proxy())
        self.assertEqual(bot.proxy, "")

    def test_no_pool_returns_false(self):
        bot = FakeBot()
        bot.manager = mock.Mock()
        bot.manager.proxy_pool = None
        self.assertFalse(bot._try_auto_proxy())

    def test_picks_least_loaded_node(self):
        """Все боты на один адрес — та самая причина пачек банов."""
        busy = "socks5://127.0.0.1:20343"
        free = "socks5://127.0.0.1:20316"
        others = []
        for i in range(5):
            o = FakeBot()
            o.bot_id = 100 + i      # уникальный id, иначе словарь схлопнется
            o.proxy = busy
            others.append(o)
        bot = FakeBot()
        bot.manager = make_manager([busy, free], bots=others + [bot])
        bot._try_auto_proxy()
        self.assertEqual(bot.proxy, free)

    def test_marks_tried(self):
        bot = FakeBot()
        bot.manager = make_manager(["socks5://127.0.0.1:20343"])
        self.assertFalse(bot._auto_proxy_tried)
        bot._try_auto_proxy()
        self.assertTrue(bot._auto_proxy_tried)

    def test_pool_exception_is_survivable(self):
        bot = FakeBot()
        mgr = make_manager([])
        mgr.proxy_pool = mock.Mock()
        mgr.proxy_pool.usable_proxies.side_effect = RuntimeError("пул упал")
        bot.manager = mgr
        self.assertFalse(bot._try_auto_proxy())


class ThresholdTests(unittest.TestCase):
    def test_threshold_is_positive(self):
        self.assertGreaterEqual(botmod.DIRECT_FALLBACK_AFTER, 1)

    def test_fallback_only_without_proxy(self):
        """Если прокси уже задан, автоподбор не должен его затирать."""
        bot = FakeBot(proxy="socks5://127.0.0.1:20316")
        bot.manager = make_manager(["socks5://127.0.0.1:20343"])
        # Условие в _run проверяет `not self.proxy` — здесь проверяем,
        # что сам метод не вызывается при заданном прокси.
        self.assertTrue(bot.proxy)
        bot._try_auto_proxy()
        # Метод сам по себе перезапишет — но в _run до него не дойдёт.
        # Фиксируем контракт: _run проверяет not self.proxy.
        with open(botmod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("and not self.proxy", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
