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


class IpBlockTextTests(unittest.TestCase):
    """Текст античита про IP — это блокировка адреса, а не бан аккаунта.

    Их нельзя путать: аккаунт после блокировки по IP цел и заходит с
    другого адреса, а если пометить его забаненным — потеряем рабочий
    аккаунт из-за чужой проблемы с IP.
    """

    def test_detects_real_anticheat_message(self):
        from logic.baninfo import is_ip_block
        text = ("Anti-Cheat: Ваш IP-адрес был заблокирован. Если Вы считаете "
                "что блокировка была выдана ошибочно обратитесь в "
                "технический раздел.")
        self.assertTrue(is_ip_block(text))

    def test_detects_with_color_codes(self):
        from logic.baninfo import is_ip_block
        text = "{FF0000}Anti-Cheat{FFFFFF}: Ваш {FFFF00}IP-адрес{FFFFFF} был заблокирован"
        self.assertTrue(is_ip_block(text))

    def test_detects_short_forms(self):
        from logic.baninfo import is_ip_block
        for text in ("Ваш IP-адрес был заблокирован",
                     "IP адрес заблокирован",
                     "Блокировка по IP",
                     "Заблокирован ваш IP"):
            self.assertTrue(is_ip_block(text), text)

    def test_does_not_match_account_ban(self):
        from logic.baninfo import is_ip_block
        for text in ("Ваш игровой аккаунт заблокирован, вход невозможен.",
                     "Администратор X навсегда заблокировал игрока Y. Причина: BOT",
                     "Вы были заблокированы администратором",
                     "Ваш аккаунт забанен за читы"):
            self.assertFalse(is_ip_block(text), text)

    def test_does_not_match_chatter(self):
        from logic.baninfo import is_ip_block
        for text in ("Информация о работах", "Сервер переполнен",
                     "IP сервера: 80.66.82.20", ""):
            self.assertFalse(is_ip_block(text), text)


class BridgePortBanTests(unittest.TestCase):
    """Бан по строке socks5://127.0.0.1:<порт> должен попадать в узел.

    Аккаунт хранит порт моста, а не узел. Раньше пометка уходила в
    выдуманный «127.0.0.1:23699», и настоящий VLESS-узел продолжал
    раздаваться — то есть бан по IP не работал вообще.
    """

    def test_bridge_port_bans_underlying_node(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            node = _node("kaz.example.com", 443)
            pool.results[node.key] = _ok(node)

            class FakeBridge:
                def key_of_port(self, port):
                    return node.key if int(port) == 23699 else ""

            pool.bridge = FakeBridge()
            pool._cached_nodes = [node]

            keys = pool.mark_banned("socks5://127.0.0.1:23699",
                                    reason="Античит: IP заблокирован")
            # Помечаются и узел, и сама строка порта — так надёжнее:
            # узел выпадает из раздачи, а старый прокси в аккаунте
            # перестаёт проходить проверку.
            self.assertIn(node.key, keys,
                          "должен пометиться узел, а не только порт моста")
            self.assertTrue(pool.is_banned("socks5://127.0.0.1:23699"))
            self.assertTrue(pool.is_banned(node.key))
            # И сам узел выпадает из раздачи.
            self.assertEqual(len(pool.usable()), 0)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class BridgePortRefreshTests(unittest.TestCase):
    """Порт моста меняется после перезапуска — аккаунт должен найти узел.

    Аккаунт хранил только socks5://127.0.0.1:<порт>. После перезапуска
    тот же узел оказывался на другом порту, и бот шёл в чужой или мёртвый
    туннель: «сервер не отвечает» при живом узле.
    """

    def test_port_reresolved_by_node_key(self):
        import bot as botmod
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "pool.json")
            pool = ProxyPool(path, prober=lambda n, **k: [], clock=lambda: 1.0)
            # vless, а не socks5: только такие узлы живут за мостом и
            # только у них порт меняется при перезапуске.
            node = VpnNode(name="kaz", protocol="vless",
                           address="kaz.example.com", port=443,
                           source="sub")
            pool.results[node.key] = _ok(node)
            pool._cached_nodes = [node]

            new_port = {"value": 25001}

            class FakeBridge:
                def available(self):
                    return True

                def key_of_port(self, port):
                    return node.key if int(port) == new_port["value"] else ""

                def port_of(self, key):
                    return new_port["value"] if key == node.key else None

                def start(self, key, link):
                    # Пул поднимает мост и получает новый порт.
                    return (f"socks5://127.0.0.1:{new_port['value']}"
                            if key == node.key else "")

            pool.bridge = FakeBridge()

            class FakeBot:
                proxy = "socks5://127.0.0.1:23699"   # старый порт
                proxy_node = node.key
                logs = []

                def add_log(self, text):
                    self.logs.append(text)

            class Mgr:
                proxy_pool = pool

            fake = FakeBot()
            result = botmod.BotManager.refresh_bridge_proxy(Mgr(), fake)
            self.assertEqual(result, "socks5://127.0.0.1:25001")
            self.assertEqual(fake.proxy, "socks5://127.0.0.1:25001")

            # Порт сменился ещё раз — пересчёт должен догнать.
            new_port["value"] = 25099
            result = botmod.BotManager.refresh_bridge_proxy(Mgr(), fake)
            self.assertEqual(result, "socks5://127.0.0.1:25099")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_manual_proxy_untouched(self):
        import bot as botmod

        class FakeBot:
            proxy = "socks5://1.2.3.4:1080"
            proxy_node = ""

            def add_log(self, text):
                pass

        class Mgr:
            proxy_pool = None

        fake = FakeBot()
        self.assertEqual(
            botmod.BotManager.refresh_bridge_proxy(Mgr(), fake),
            "socks5://1.2.3.4:1080")


class IpBanHandlerTests(unittest.TestCase):
    """handle_ip_ban: адрес в мусор, аккаунты целы и припаркованы.

    Блокировка по IP — это не бан аккаунта. Раньше обработчик помечал
    аккаунт `is_banned` и гасил сессию через stop(), и владелец видел
    «аккаунты быстро банятся и вылетают». Теперь метка ложится на адрес
    узла, а аккаунт уходит на парковку.
    """

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

    def _bot(self, bid, proxy, nickname, proxy_node="", live=False):
        class B:
            def __init__(self):
                self.bot_id = bid
                self.proxy = proxy
                self.proxy_node = proxy_node
                self.nickname = nickname
                self.is_banned = False
                self.ban_reason = ""
                self.ban_kind = ""
                self.banned_at = 0.0
                self.parked_until = 0.0
                self.parked_reason = ""
                self.status = ""
                self.logs = []
                self.stopped = False
                self.stop_calls = 0
                if live:
                    self.session = type("S", (), {"connected": True})()

            def add_log(self, text):
                self.logs.append(text)

            def cancel_reconnect(self):
                pass

            def stop(self, banned=False):
                # Считаем вызовы: аккаунт при бане АДРЕСА гасить нельзя.
                self.stop_calls += 1
                self.stopped = banned

        return B()

    def test_only_accounts_on_that_proxy_touched(self):
        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        b = self._bot(2, "socks5://9.9.9.9:1080", "B")
        c = self._bot(3, "socks5://8.8.8.8:1080", "C")

        pool = self.pool
        handled = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a, 2: b, 3: c}
                self.proxy_pool = pool
                self.tg_controller = None
                self.saved = 0

            def save_accounts(self):
                self.saved += 1

            def swap_proxy(self, bot, *, reason=""):
                handled.append(bot.bot_id)
                return ""

        mgr = Mgr()
        out = self.botmod.BotManager.handle_ip_ban(mgr, a, reason="connection banned")

        self.assertEqual(sorted(handled), [1, 2],
                         "тронут только адрес 9.9.9.9, аккаунт на 8.8.8.8 не при чём")
        self.assertNotIn(3, handled)
        self.assertTrue(self.pool.is_banned("socks5://9.9.9.9:1080"))
        self.assertEqual(mgr.saved, 1)
        # Замены нет — аккаунты не забанены, а припаркованы.
        self.assertEqual(sorted(out["parked"]), [1, 2])
        self.assertFalse(a.is_banned)
        self.assertFalse(b.is_banned)
        self.assertGreater(a.parked_until, time.time())

    def test_accounts_moved_to_other_proxy(self):
        """IP-бан переводит аккаунты на другой узел, а не убивает их.

        Аккаунт при блокировке по IP цел — виноват адрес. Если помечать
        аккаунт забаненным, мы теряем рабочий аккаунт из-за чужой
        проблемы с IP.
        """
        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        b = self._bot(2, "socks5://9.9.9.9:1080", "B")

        spare = _node("5.5.5.5")
        self.pool.results[spare.key] = _ok(spare)

        started = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a, 2: b}
                self.proxy_pool = self_pool
                self.tg_controller = None

            def save_accounts(self):
                pass

            def swap_proxy(self, bot, *, reason=""):
                bot.proxy = "socks5://5.5.5.5:1080"
                started.append(bot.bot_id)
                return bot.proxy

        self_pool = self.pool
        mgr = Mgr()
        out = self.botmod.BotManager.handle_ip_ban(mgr, a, reason="Античит: IP заблокирован")

        self.assertEqual(sorted(out["moved"]), [1, 2])
        self.assertEqual(out["parked"], [])
        self.assertFalse(a.is_banned, "аккаунт не должен считаться забаненным")
        self.assertFalse(b.is_banned)
        self.assertEqual(a.proxy, "socks5://5.5.5.5:1080")
        self.assertTrue(self.pool.is_banned("socks5://9.9.9.9:1080"))

    def test_no_replacement_parks_account_instead_of_banning(self):
        """Замены нет — аккаунт припаркован, жив и не помечен забаненным.

        Раньше здесь стоял `is_banned = True`, `ban_kind = "ip"` и
        `status = "ЗАБАНЕН (IP)"`. Из-за закрытого адреса терялся живой
        аккаунт — на это и жаловался владелец.
        """
        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        stopped = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a}
                self.proxy_pool = self_pool
                self.tg_controller = None

            def save_accounts(self):
                pass

            def swap_proxy(self, bot, *, reason=""):
                return ""

        self_pool = self.pool
        out = self.botmod.BotManager.handle_ip_ban(Mgr(), a, reason="ip")
        self.assertEqual(out["moved"], [])
        self.assertEqual(out["parked"], [1])
        # Адрес — в мусор.
        self.assertTrue(self.pool.is_banned("socks5://9.9.9.9:1080"))
        # Аккаунт — жив.
        self.assertFalse(a.is_banned, "бан адреса не банит аккаунт")
        self.assertEqual(a.ban_kind, "")
        self.assertNotEqual(a.status, "ЗАБАНЕН (IP)")
        # Сессию не гасим.
        self.assertEqual(a.stop_calls, 0, "живую сессию stop() не трогает")
        # И припаркован с понятной причиной.
        self.assertGreater(a.parked_until, time.time())
        self.assertIn("заблокирован", a.parked_reason)

    def test_live_session_is_not_stopped(self):
        """Идёт игра — сессию не гасим, даже если адрес закрыт."""
        a = self._bot(1, "socks5://9.9.9.9:1080", "A", live=True)

        class Mgr:
            def __init__(self):
                self.bots = {1: a}
                self.proxy_pool = self_pool
                self.tg_controller = None
                self.swapped = []

            def save_accounts(self):
                pass

            def swap_proxy(self, bot, *, reason=""):
                self.swapped.append(bot.bot_id)
                return "socks5://5.5.5.5:1080"

        self_pool = self.pool
        mgr = Mgr()
        out = self.botmod.BotManager.handle_ip_ban(mgr, a, reason="ip")
        self.assertEqual(out["parked"], [1])
        self.assertEqual(a.stop_calls, 0, "живую сессию не гасим")
        self.assertEqual(mgr.swapped, [], "переезд не трогает идущую сессию")
        self.assertFalse(a.is_banned)
        self.assertGreater(a.parked_until, time.time())

    def test_park_is_targeted_by_node_not_by_bridge_url(self):
        """Мост 127.0.0.1:10808 один на всех — парковка идёт по УЗЛУ.

        Иначе один закрытый узел гасит всю пачку аккаунтов за общим мостом.
        """
        bridge = "socks5://127.0.0.1:10808"
        a = self._bot(1, bridge, "A", proxy_node="node-1")
        b = self._bot(2, bridge, "B", proxy_node="node-2")
        c = self._bot(3, bridge, "C", proxy_node="")

        pool = self.pool
        handled = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a, 2: b, 3: c}
                self.proxy_pool = pool
                self.tg_controller = None

            def save_accounts(self):
                pass

            def swap_proxy(self, bot, *, reason=""):
                handled.append(bot.bot_id)
                return ""

        out = self.botmod.BotManager.handle_ip_ban(Mgr(), a, reason="ip")
        self.assertEqual(out["parked"], [1], "узел node-1, а не весь мост")
        self.assertNotIn(2, out["parked"], "другой узел за тем же мостом не при чём")
        self.assertNotIn(3, out["parked"], "без узла бота не паркуем")
        self.assertFalse(b.is_banned)
        self.assertFalse(c.is_banned)

    def test_connection_banned_text_reaches_handler_and_does_not_ban(self):
        """`Пакет CONNECTION_BANNED (0x19)` — капс не мешает, аккаунт цел.

        Текстовый вход нужен для чата и окна Anti-Cheat: там кода нет,
        только строка. Раньше этот же путь ставил аккаунту `is_banned`,
        `ban_kind="ip"` и гасил сессию.
        """
        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        seen = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a}
                self.proxy_pool = self_pool
                self.tg_controller = None

            def save_accounts(self):
                pass

            def handle_ip_ban(self, bot, reason=""):
                seen.append((bot.bot_id, reason))
                return {"proxy": "", "banned_keys": [], "moved": [], "parked": []}

            def add_bot(self, **kw):
                raise AssertionError("замена при бане адреса не создаётся")

        self_pool = self.pool
        self.botmod.BotManager.mark_bot_banned(
            Mgr(), a, reason="Пакет CONNECTION_BANNED (0x19)")

        self.assertEqual([s[0] for s in seen], [1],
                         "капс в причине не мешает: 0x19 обязан дойти "
                         "до обработчика адреса")
        self.assertFalse(a.is_banned, "бан адреса не банит аккаунт")
        self.assertEqual(a.ban_kind, "")

    def test_kick_999_goes_by_code_and_park_account(self):
        """Кик 999 (CONNECTION_BANNED) решается по КОДУ, не по тексту.

        Хвост сообщений сервера в причине не должен ломать решение:
        раньше ветка 999 звала mark_bot_banned и банила аккаунт.
        """
        import bot as botmod
        from logic.events import emitter

        a = self._bot(1, "socks5://9.9.9.9:1080", "A")
        seen = []

        mgr = botmod.BotManager.__new__(botmod.BotManager)
        mgr.bots = {1: a}
        mgr._lock = threading.Lock()
        mgr._obj_index = {id(a): 1}
        mgr.proxy_pool = self.pool
        mgr.tg_controller = None
        mgr.handle_ip_ban = lambda bot, reason="": seen.append(reason) or {
            "proxy": "", "banned_keys": [], "moved": [], "parked": []}

        # Обработчики живут в глобальном эмиттере: снимаем после себя
        # целиком, вместе с чужими, которые были до нас.
        snapshot = {k: list(v) for k, v in emitter._listeners.items()}
        mgr._setup_event_hooks()
        try:
            emitter.emit("onKick", a, 999)
            for _ in range(300):
                if seen:
                    break
                time.sleep(0.01)
        finally:
            emitter._listeners.clear()
            emitter._listeners.update(snapshot)

        self.assertEqual(len(seen), 1, "999 обязан дойти до обработчика адреса")
        self.assertIn("CONNECTION_BANNED", seen[0])
        self.assertFalse(a.is_banned, "999 — бан адреса, аккаунт не баним")

    def test_watchdog_does_not_stop_live_session(self):
        """Сторож прокси не гасит идущую игру из-за бана адреса.

        Иначе вред из handle_ip_ban просто переехал бы в сторож: он тоже
        видит `is_banned(proxy)` и зовёт swap_proxy со stop()+start().
        """
        a = self._bot(1, "socks5://9.9.9.9:1080", "A", live=True)
        a.stopping = False
        self.pool.mark_banned("socks5://9.9.9.9:1080", reason="тест")

        swapped = []

        class Mgr:
            def __init__(self):
                self.bots = {1: a}
                self.proxy_pool = self_pool

            def swap_proxy(self, bot, *, reason=""):
                swapped.append(bot.bot_id)
                return ""

        self_pool = self.pool
        self.botmod.BotManager._proxy_watch_pass(Mgr())

        self.assertEqual(swapped, [], "живую сессию сторож не трогает")
        self.assertEqual(a.stop_calls, 0, "stop() посреди игры запрещён")
        self.assertFalse(a.is_banned, "бан адреса не банит аккаунт")
        self.assertGreater(a.parked_until, time.time())

    def test_no_proxy_does_not_crash(self):
        a = self._bot(1, "", "A")

        class Mgr:
            bots = {1: a}
            proxy_pool = None
            tg_controller = None

            def save_accounts(self):
                pass

        out = self.botmod.BotManager.handle_ip_ban(Mgr(), a, reason="x")
        self.assertEqual(out["moved"], [])
        self.assertEqual(out["parked"], [])
        self.assertEqual(out["proxy"], "")


class ParkingTests(unittest.TestCase):
    """Парковка: аккаунт жив, но в закрытый адрес не долбится."""

    def test_park_delays_reconnect_start(self):
        from test_reconnect import make_bot

        bot = make_bot()
        bot.parked_until = time.time() + 2.0
        bot.parked_reason = "адрес заблокирован, свободного узла нет"

        bot.schedule_reconnect(delay=0.1)
        time.sleep(1.0)
        self.assertFalse(bot.start.called,
                         "пока парковка идёт, вход не начинается")

        for _ in range(60):
            if bot.start.called:
                break
            time.sleep(0.1)
        self.assertTrue(bot.start.called,
                        "после парковки вход обязан состояться")

        bot.stopping = True
        bot.reconnect_cancel_event.set()

    def test_unpark_allows_immediate_reconnect(self):
        from test_reconnect import make_bot

        bot = make_bot()
        bot.parked_until = time.time() + 600.0
        bot.parked_reason = "долгая парковка"
        bot.unpark("ручной запуск")
        self.assertEqual(bot.park_remaining(), 0.0)
        self.assertFalse(bot.is_parked())


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
