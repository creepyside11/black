"""Веб-панель: мост команд, авторизация, HTTP.

Главное, что тут проверяется — панель не лезет в аккаунты сама. Она кладёт
команду в очередь, её исполняет бот теми же методами, что и Telegram.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from logic import web_bridge
from web import server as web_server
from web.auth import Auth, generate_password


class TempDirMixin:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rakcrmp_")
        self._orig = (web_bridge.BASE_DIR, web_bridge.INBOX,
                      web_bridge.OUTBOX, web_bridge.STATE_FILE)
        web_bridge.BASE_DIR = self.tmp
        web_bridge.INBOX = os.path.join(self.tmp, "inbox")
        web_bridge.OUTBOX = os.path.join(self.tmp, "outbox")
        web_bridge.STATE_FILE = os.path.join(self.tmp, "state.json")
        web_bridge._ensure_dirs()

    def tearDown(self):
        (web_bridge.BASE_DIR, web_bridge.INBOX,
         web_bridge.OUTBOX, web_bridge.STATE_FILE) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakeBot:
    def __init__(self, bot_id, nickname, banned=False, task="lvl"):
        self.bot_id = bot_id
        self.nickname = nickname
        self.server_name = "TVER"
        self.status = "В игре (заспавнен)"
        self.is_banned = banned
        self.level_str = "3 lvl"
        self.money = 1500
        self.default_task_name = task
        self.active_task = None
        self.proxy = ""
        self.logs = ["лог 1"]
        self.api = mock.Mock()
        self.api.getbotposition.return_value = (1.0, 2.0, 3.0)


class FakeManager:
    def __init__(self, bots=None):
        self.bots = {b.bot_id: b for b in (bots or [])}
        self.proxy_pool = None
        self.settings = None
        self.saved = 0
        self.started = []
        self.stopped = []
        self.deleted = []
        self.unbanned = []

    def save_accounts(self):
        self.saved += 1

    def start_bot(self, bot_id):
        self.started.append(bot_id)

    def stop_bot(self, bot_id):
        self.stopped.append(bot_id)

    def delete_bot(self, bot_id):
        self.deleted.append(bot_id)
        self.bots.pop(bot_id, None)

    def unban_bot(self, bot_id):
        self.unbanned.append(bot_id)
        self.bots[bot_id].is_banned = False

    def add_bot(self, server, nickname, password, default_task="lvl", proxy=""):
        bot = FakeBot(max(self.bots) + 1 if self.bots else 1,
                      nickname or "Generated_One")
        self.bots[bot.bot_id] = bot
        return bot


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rakcrmp_auth_")
        self.path = os.path.join(self.tmp, "auth.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_password_is_generated_not_chosen(self):
        pw = generate_password()
        self.assertEqual(len(pw), 20)
        self.assertNotIn("O", pw)
        self.assertNotIn("0", pw)
        self.assertNotIn("l", pw)

    def test_secrets_are_not_stored_in_plaintext(self):
        auth = Auth(self.path)
        login, password = auth.issue()
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(password, raw, "пароль лежит на диске открытым текстом")
        self.assertIn("hash", raw)

    def test_verify(self):
        auth = Auth(self.path)
        login, password = auth.issue()
        self.assertTrue(auth.verify(login, password))
        self.assertFalse(auth.verify(login, "не тот"))
        self.assertFalse(auth.verify("чужой", password))

    def test_reissue_kills_old_password(self):
        auth = Auth(self.path)
        login, old = auth.issue()
        token = auth.start_session()
        auth.issue(login)
        self.assertFalse(auth.verify(login, old))
        self.assertFalse(auth.check_session(token), "старая сессия пережила смену пароля")

    def test_session_expires(self):
        auth = Auth(self.path)
        auth.issue()
        token = auth.start_session()
        self.assertTrue(auth.check_session(token))
        auth._sessions[token] = time.time() - 1
        self.assertFalse(auth.check_session(token))

    def test_unconfigured_rejects_everything(self):
        auth = Auth(self.path)
        self.assertFalse(auth.configured)
        self.assertFalse(auth.verify("a", "b"))


class BridgeTests(TempDirMixin, unittest.TestCase):
    def _bridge(self, manager):
        return web_bridge.WebBridge(manager, log=lambda _m: None)

    def _run_once(self, bridge):
        bridge._drain()

    def test_submit_times_out_without_bot(self):
        reply = web_bridge.submit("ping", {}, timeout=0.6)
        self.assertFalse(reply["ok"])
        self.assertIn("не ответил", reply["error"])

    def test_ping_executes(self):
        manager = FakeManager()
        bridge = self._bridge(manager)
        result = {}

        def caller():
            result.update(web_bridge.submit("ping", {}, timeout=5.0))

        thread = threading.Thread(target=caller)
        thread.start()
        deadline = time.time() + 4
        while thread.is_alive() and time.time() < deadline:
            self._run_once(bridge)
            time.sleep(0.05)
        thread.join(timeout=3)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result["result"]["pong"])

    def test_start_and_stop_go_through_manager(self):
        manager = FakeManager([FakeBot(5, "Test_Nick")])
        bridge = self._bridge(manager)
        bridge._execute("start", {"id": 5})
        bridge._execute("stop", {"id": 5})
        self.assertEqual(manager.started, [5])
        self.assertEqual(manager.stopped, [5])

    def test_unknown_command_is_rejected(self):
        bridge = self._bridge(FakeManager())
        with self.assertRaises(ValueError):
            bridge._execute("drop_everything", {})

    def test_missing_bot_is_reported(self):
        bridge = self._bridge(FakeManager())
        with self.assertRaises(ValueError):
            bridge._execute("start", {"id": 999})

    def test_bot_payload_has_no_secrets(self):
        manager = FakeManager([FakeBot(1, "Nick_One")])
        bridge = self._bridge(manager)
        payload = bridge._bots_payload()
        self.assertEqual(len(payload), 1)
        bot = payload[0]
        self.assertEqual(bot["nickname"], "Nick_One")
        self.assertNotIn("password", bot, "пароль аккаунта утёк в панель")
        self.assertNotIn("network", bot)

    def test_bot_payload_shows_point(self):
        """Панель должна показывать, куда движок поставил бота."""
        bot = FakeBot(3, "Nick_Three")
        bot.lvl_spawn_name = "Нижегородск 2"
        bot.lvl_target = [-2125.721, 1311.702, 11.987]
        bot.lvl_target_index = 1
        bot.lvl_phase = "done"
        bridge = self._bridge(FakeManager([bot]))
        payload = bridge._bots_payload()[0]
        self.assertEqual(payload["lvl"]["station"], "Нижегородск 2")
        self.assertEqual(payload["lvl"]["target_index"], 1)
        self.assertEqual(payload["lvl"]["phase"], "done")

    def test_snapshot_written(self):
        manager = FakeManager([FakeBot(2, "Nick_Two")])
        bridge = self._bridge(manager)
        bridge._snapshot()
        with open(web_bridge.STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["bots"][0]["nickname"], "Nick_Two")

    def test_stale_files_are_swept(self):
        path = os.path.join(web_bridge.INBOX, "old.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"id": "old", "cmd": "ping"}, fh)
        old = time.time() - web_bridge.STALE_SECONDS - 10
        os.utime(path, (old, old))
        self._bridge(FakeManager())._sweep()
        self.assertFalse(os.path.exists(path))

    def test_broken_command_file_does_not_kill_bridge(self):
        path = os.path.join(web_bridge.INBOX, "broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        bridge = self._bridge(FakeManager())
        bridge._drain()  # не должно бросить
        self.assertTrue(os.path.exists(path))


class BulkTests(TempDirMixin, unittest.TestCase):
    """Пачки: выделил несколько аккаунтов — применилось ко всем."""

    def _bridge(self, manager):
        return web_bridge.WebBridge(manager, log=lambda _m: None)

    def _wait(self, condition, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_start_many_starts_every_selected(self):
        manager = FakeManager([FakeBot(1, "A"), FakeBot(2, "B"), FakeBot(3, "C")])
        bridge = self._bridge(manager)
        result = bridge._execute("start_many", {"ids": [1, 3]})
        self.assertEqual(result["count"], 2)
        self.assertTrue(self._wait(lambda: sorted(manager.started) == [1, 3]),
                        manager.started)
        self.assertNotIn(2, manager.started)

    def test_stop_many_stops_every_selected(self):
        manager = FakeManager([FakeBot(4, "D"), FakeBot(5, "E")])
        bridge = self._bridge(manager)
        bridge._execute("stop_many", {"ids": [4, 5]})
        self.assertTrue(self._wait(lambda: sorted(manager.stopped) == [4, 5]),
                        manager.stopped)

    def test_delete_many_removes_from_registry(self):
        manager = FakeManager([FakeBot(7, "G"), FakeBot(8, "H"), FakeBot(9, "I")])
        bridge = self._bridge(manager)
        bridge._execute("delete_many", {"ids": [7, 9]})
        self.assertTrue(self._wait(lambda: sorted(manager.deleted) == [7, 9]),
                        manager.deleted)
        self.assertEqual(sorted(manager.bots), [8])

    def test_unknown_ids_are_skipped_not_fatal(self):
        manager = FakeManager([FakeBot(1, "A")])
        bridge = self._bridge(manager)
        result = bridge._execute("start_many", {"ids": [1, 999]})
        self.assertEqual(result["queued"], [1])
        self.assertEqual(result["skipped"], [999])

    def test_empty_ids_rejected(self):
        bridge = self._bridge(FakeManager([FakeBot(1, "A")]))
        with self.assertRaises(ValueError):
            bridge._execute("start_many", {"ids": []})

    def test_bulk_does_not_block_the_bridge(self):
        # Полсотни аккаунтов: команда обязана вернуться сразу, иначе
        # панель отвалится по таймауту, пока бот подключается.
        bots = [FakeBot(i, f"B{i}") for i in range(1, 51)]
        manager = FakeManager(bots)
        bridge = self._bridge(manager)
        started = time.monotonic()
        bridge._execute("start_many", {"ids": [b.bot_id for b in bots]})
        self.assertLess(time.monotonic() - started, 1.0)

    def test_payload_carries_level_and_exp(self):
        bot = FakeBot(1, "A")
        bot.level_num = 3
        bot.exp_cur = 12
        bot.exp_next = 40
        bot.exp_left = 28
        bot.level_str = "3 lvl · 12/40"
        bridge = self._bridge(FakeManager([bot]))
        payload = bridge._bots_payload()[0]
        self.assertEqual(payload["level_num"], 3)
        self.assertEqual(payload["exp_cur"], 12)
        self.assertEqual(payload["exp_next"], 40)
        self.assertEqual(payload["exp_left"], 28)
        self.assertEqual(payload["exp_percent"], 30.0)
        self.assertEqual(payload["level"], "3 lvl · 12/40")


class ServerTests(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.auth_path = os.path.join(self.tmp, "auth.json")
        self.auth = Auth(self.auth_path)
        self.login, self.password = self.auth.issue()
        self.manager = FakeManager([FakeBot(1, "Alpha")])
        self.bridge = web_bridge.WebBridge(self.manager, log=lambda _m: None)
        self.bridge.running = True

        self.server = web_server.PanelServer(("127.0.0.1", 0), web_server.Handler)
        self.server.app = web_server.App(self.auth)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # Исполняем очередь в фоне, как это делает бот.
        self.bridge_thread = threading.Thread(target=self._bridge_loop, daemon=True)
        self.bridge_thread.start()

    def _bridge_loop(self):
        while self.bridge.running:
            try:
                self.bridge._drain()
                self.bridge._snapshot()
            except Exception:
                pass
            time.sleep(0.03)

    def tearDown(self):
        self.bridge.running = False
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _request(self, path, method="GET", body=None, cookie=None):
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read() or b"{}"
                try:
                    payload = json.loads(raw)
                except Exception:
                    # Статика — не JSON. Отдаём сырое тело как есть.
                    payload = {"_raw": raw.decode("utf-8", "ignore")}
                return res.status, payload, res.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            return exc.code, payload, exc.headers

    def test_static_index_served(self):
        status, payload, _ = self._request("/")
        self.assertEqual(status, 200)
        body = payload.get("_raw", "")
        self.assertIn("RakCRMP", body, "index.html не отдался")
        self.assertIn("/app.js", body)

    def test_api_requires_login(self):
        status, payload, _ = self._request("/api/state")
        self.assertEqual(status, 401)
        self.assertIn("вход", payload["error"])

    def test_wrong_password_rejected(self):
        status, payload, _ = self._request(
            "/api/login", "POST", {"login": self.login, "password": "нет"})
        self.assertEqual(status, 401)

    def test_login_then_state(self):
        status, payload, headers = self._request(
            "/api/login", "POST", {"login": self.login, "password": self.password})
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        self.assertIn("rakcrmp_session", cookie)

        status, payload, _ = self._request("/api/state", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["bots"][0]["nickname"], "Alpha")

    def test_command_reaches_manager(self):
        _, _, headers = self._request(
            "/api/login", "POST", {"login": self.login, "password": self.password})
        cookie = headers.get("Set-Cookie", "").split(";")[0]

        status, payload, _ = self._request(
            "/api/start", "POST", {"id": 1}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        time.sleep(0.2)
        self.assertEqual(self.manager.started, [1])

    def test_bulk_command_reaches_manager(self):
        _, _, headers = self._request(
            "/api/login", "POST", {"login": self.login, "password": self.password})
        cookie = headers.get("Set-Cookie", "").split(";")[0]

        self.manager.bots[2] = FakeBot(2, "Beta")
        status, payload, _ = self._request(
            "/api/start_many", "POST", {"ids": [1, 2]}, cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["count"], 2)
        deadline = time.time() + 3
        while time.time() < deadline and sorted(self.manager.started) != [1, 2]:
            time.sleep(0.02)
        self.assertEqual(sorted(self.manager.started), [1, 2])

    def test_command_without_login_refused(self):
        status, _, _ = self._request("/api/start", "POST", {"id": 1})
        self.assertEqual(status, 401)
        self.assertEqual(self.manager.started, [])

    def test_unknown_command_refused(self):
        _, _, headers = self._request(
            "/api/login", "POST", {"login": self.login, "password": self.password})
        cookie = headers.get("Set-Cookie", "").split(";")[0]
        status, _, _ = self._request("/api/nuke", "POST", {}, cookie=cookie)
        self.assertEqual(status, 404)

    def test_brute_force_is_throttled(self):
        app = self.server.app
        for _ in range(8):
            self.assertTrue(app.allow_attempt("1.2.3.4"))
        self.assertFalse(app.allow_attempt("1.2.3.4"))
        self.assertTrue(app.allow_attempt("5.6.7.8"), "чужой IP не должен страдать")

    def test_path_traversal_blocked(self):
        # Ни /etc/passwd, ни auth.json наружу не уходят: неизвестный путь
        # отдаёт index.html, а не файл с диска.
        for path in ("/../../etc/passwd", "/../auth.json", "/../bot.py"):
            status, payload, _ = self._request(path)
            body = payload.get("_raw", "")
            self.assertNotIn("root:x:", body, f"{path} отдал /etc/passwd")
            self.assertNotIn("hash", body, f"{path} отдал auth.json")
            self.assertNotIn("BotManager", body, f"{path} отдал исходник")


if __name__ == "__main__":
    unittest.main(verbosity=2)
