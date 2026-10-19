"""callback_data не должен превышать 64 байта.

Telegram отвечает 400 BUTTON_DATA_INVALID и отвергает ВСЮ клавиатуру:
кнопка «Прокси и VPN» просто не открывалась, потому что в неё клался
полный ключ узла — с UUID и отпечатком конфига он длиннее лимита.
"""

import json
import os
import re
import threading
import unittest
from unittest import mock

import bot as botmod
import tg_controller as tg

LIMIT = 64
CONTROLLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_controller.py")


class StaticTests(unittest.TestCase):
    def test_no_fstring_callback_with_node_key(self):
        """Ключ узла в callback_data — та самая ошибка."""
        with open(CONTROLLER, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('callback_data=f"vpn_pick:{key}"', src)
        self.assertNotIn("callback_data=f\"vpn_pick:{node.key}\"", src)


class RenderedLimitTests(unittest.TestCase):
    def setUp(self):
        self.manager = botmod.BotManager.__new__(botmod.BotManager)
        self.manager._lock = threading.RLock()
        self.manager.bots = {}
        self.manager.settings = None

        self.ctrl = tg.TelegramBotController.__new__(tg.TelegramBotController)
        self.ctrl.token = "1:a"
        self.ctrl.manager = self.manager
        self.ctrl.admin_ids = [1]
        self.ctrl.admin_id = 1
        self.ctrl.active_chats = {1}
        self.ctrl.user_states = {}
        self.ctrl._live_views = {}
        self.ctrl._views_lock = threading.RLock()
        self.ctrl._live_thread = None
        self.ctrl._vpn_picks = {}
        self.ctrl.running = False
        self.ctrl.bot = mock.Mock()
        self.sent = []
        self.ctrl.bot.send_message.side_effect = (
            lambda cid, text, reply_markup=None, **k: self.sent.append(reply_markup))

    def _long_node(self, i):
        from logic.vpn_sub import VpnNode
        return VpnNode(
            name="🇩🇪 Очень длинное имя узла номер %d" % i,
            protocol="vless", address="site.babay.su", port=443,
            security="reality",
            login="84d2af53-66b1-475a-801c-3acd0178b887",
            raw='{"outbounds":[{"server":"site.babay.su","sni":"x%d"}]}' % i,
        )

    def test_menu_buttons_fit(self):
        from logic import proxy_pool as P
        from logic.vpn_probe import ProbeResult, STATUS_OK

        nodes = [self._long_node(i) for i in range(16)]
        pool = P.ProxyPool("/tmp/_cb_test.json", loader=lambda u, **k: [],
                           log=lambda _m: None)
        pool.bridge = None
        pool._cached_nodes = nodes
        pool.results = {}
        for n in nodes:
            pool.results[n.key] = ProbeResult(node=n, alive=True, usable=True,
                                              status=STATUS_OK)
        pool.statuses = lambda: list(pool.results.values())
        self.manager.proxy_pool = pool

        self.ctrl._render_vpn_menu(1)
        markup = self.sent[-1]
        self.assertIsNotNone(markup, "меню не отрисовалось")
        for row in markup.keyboard:
            for button in row:
                data = button.callback_data or ""
                self.assertLessEqual(
                    len(data.encode("utf-8")), LIMIT,
                    f"кнопка «{button.text}»: {len(data.encode('utf-8'))} байт")
        os.path.exists("/tmp/_cb_test.json") and os.unlink("/tmp/_cb_test.json")

    def test_picks_map_resolves(self):
        from logic import proxy_pool as P
        from logic.vpn_probe import ProbeResult, STATUS_OK

        node = self._long_node(0)
        pool = P.ProxyPool("/tmp/_cb_test2.json", loader=lambda u, **k: [],
                           log=lambda _m: None)
        pool.bridge = None
        pool._cached_nodes = [node]
        pool.results = {node.key: ProbeResult(node=node, alive=True, usable=True,
                                              status=STATUS_OK)}
        pool.statuses = lambda: list(pool.results.values())
        self.manager.proxy_pool = pool

        self.ctrl._render_vpn_menu(1)
        self.assertEqual(self.ctrl._vpn_picks.get("0"), node.key)
        os.path.exists("/tmp/_cb_test2.json") and os.unlink("/tmp/_cb_test2.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
