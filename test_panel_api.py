"""Точки панели для мини-приложения: онлайн серверов, логи ботов, токен.

Сеть здесь не нужна: загрузчик br_servers подменяется, логи пишутся во
временный каталог. Проверяем контракт, а не живость API.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from logic import web_bridge
from web import server as web_server
from web.auth import Auth

FIXTURE = [
    {"id": 1, "status": "", "online": "1485", "maxonline": "1300",
     "color": "FF0000", "x2": "true", "key": "1424",
     "name": "BLACK RUSSIA | RED", "firstname": "RED", "secondname": "",
     "url": "red.blackrussia.online", "ip": "80.66.82.46", "port": "5125"},
    {"id": 7, "status": "", "online": "12", "maxonline": "500",
     "color": "#00ff00", "x2": "false", "name": "BLACK RUSSIA | GREEN",
     "firstname": "GREEN", "secondname": "",
     "url": "green.blackrussia.online", "ip": "1.2.3.4", "port": "7777"},
    {"id": 9, "status": "", "online": "", "maxonline": "",
     "color": "", "x2": "", "name": "BLACK RUSSIA | BLUE",
     "firstname": "BLUE", "secondname": "",
     "url": "blue.blackrussia.online", "ip": "5.6.7.8", "port": "8888"},
]


class PanelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rakcrmp_api_")
        self._orig_state = web_bridge.STATE_FILE
        web_bridge.STATE_FILE = os.path.join(self.tmp, "state.json")
        self._orig_env = {k: os.environ.get(k) for k in ("PANEL_TOKEN", "BR_LOG_DIR")}
        os.environ.pop("PANEL_TOKEN", None)
        os.environ.pop("BR_LOG_DIR", None)

        self.auth = Auth(os.path.join(self.tmp, "auth.json"))
        self.login, self.password = self.auth.issue()
        self.server = web_server.PanelServer(("127.0.0.1", 0), web_server.Handler)
        self.server.app = web_server.App(self.auth)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        web_bridge.STATE_FILE = self._orig_state
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _request(self, path, method="GET", body=None, cookie=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read() or b"{}"
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {"_raw": raw.decode("utf-8", "ignore")}
                return res.status, payload, res.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                payload = {}
            return exc.code, payload, exc.headers

    def _login(self):
        status, _, headers = self._request(
            "/api/login", "POST", {"login": self.login, "password": self.password})
        self.assertEqual(status, 200)
        return headers.get("Set-Cookie", "").split(";")[0]

    def _write_log(self, name, text, age=0.0):
        logs = os.path.join(self.tmp, "logs")
        os.makedirs(logs, exist_ok=True)
        path = os.path.join(logs, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def _snapshot(self, bots):
        with open(web_bridge.STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"bots": bots}, fh)


class OnlineTests(PanelBase):
    def test_normalizes_numbers_bool_and_color(self):
        cookie = self._login()
        with mock.patch("br_servers.get_servers", return_value=FIXTURE):
            status, payload, _ = self._request("/api/online", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["stale"])
        servers = payload["servers"]
        self.assertEqual([s["id"] for s in servers], [1, 7, 9],
                         "не отсортировано по онлайну")
        red, green, blue = servers
        self.assertEqual(red["online"], 1485)
        self.assertIsInstance(red["online"], int)
        self.assertEqual(red["maxonline"], 1300)
        self.assertIs(red["x2"], True)
        self.assertEqual(red["color"], "#FF0000")
        self.assertEqual(green["color"], "#00FF00")
        self.assertIs(green["x2"], False)
        self.assertEqual(blue["online"], 0)
        self.assertEqual(blue["maxonline"], 0)
        self.assertIs(blue["x2"], False)
        self.assertEqual(blue["color"], "#FFFFFF")

    def test_network_down_falls_back_to_disk_cache(self):
        cookie = self._login()
        with mock.patch("br_servers.get_servers",
                        side_effect=RuntimeError("нет сети")), \
             mock.patch("br_servers._load_disk_cache", return_value=FIXTURE):
            status, payload, _ = self._request("/api/online", cookie=cookie)
        self.assertEqual(status, 200, "сеть упала — панель ответила 500")
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["servers"]), 3)
        self.assertIn("reason", payload)

    def test_no_network_and_no_cache_is_not_500(self):
        cookie = self._login()
        with mock.patch("br_servers.get_servers",
                        side_effect=RuntimeError("нет сети")), \
             mock.patch("br_servers._load_disk_cache", return_value=None):
            status, payload, _ = self._request("/api/online", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["servers"], [])

    def test_requires_auth(self):
        with mock.patch("br_servers.get_servers", return_value=FIXTURE):
            status, payload, _ = self._request("/api/online")
        self.assertEqual(status, 401)
        self.assertIn("вход", payload["error"])


class LogsTests(PanelBase):
    def setUp(self):
        super().setUp()
        # Без этого падаем в корень проекта и читаем чужие *.log.
        os.environ["BR_LOG_DIR"] = os.path.join(self.tmp, "logs")

    def test_missing_directory_is_reported_not_raised(self):
        os.environ["BR_LOG_DIR"] = os.path.join(self.tmp, "nope")
        cookie = self._login()
        status, payload, _ = self._request("/api/logs", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["bots"], [])
        self.assertIn("каталог", payload["reason"])

    def test_empty_directory_returns_empty_list(self):
        logs = os.path.join(self.tmp, "logs")
        os.makedirs(logs, exist_ok=True)
        os.environ["BR_LOG_DIR"] = logs
        cookie = self._login()
        status, payload, _ = self._request("/api/logs", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["bots"], [])
        self.assertIn("reason", payload)

    def test_per_bot_file_uses_name_and_snapshot(self):
        self._write_log("Alpha.log", "\n".join(f"строка {i}" for i in range(10)) + "\n")
        self._write_log("Beta.log", "beta 1\nbeta 2\n")
        self._snapshot([
            {"nickname": "Alpha", "server": "TVER", "status": "В игре (заспавнен)"},
            {"nickname": "Off_One", "server": "RED", "status": "Остановлен"},
        ])
        cookie = self._login()
        status, payload, _ = self._request("/api/logs", cookie=cookie)
        self.assertEqual(status, 200)
        bots = {b["nick"]: b for b in payload["bots"]}
        self.assertEqual(sorted(bots), ["Alpha", "Beta"])
        self.assertEqual(bots["Alpha"]["server"], "TVER")
        self.assertEqual(bots["Alpha"]["status"], "В игре (заспавнен)")
        self.assertIs(bots["Alpha"]["connected"], True)
        self.assertEqual(bots["Alpha"]["tail"][-1], "строка 9")
        self.assertEqual(len(bots["Alpha"]["tail"]), 10)
        self.assertEqual(bots["Beta"]["tail"], ["beta 1", "beta 2"])
        self.assertEqual(set(bots["Alpha"]),
                         {"nick", "server", "status", "connected", "tail"})

    def test_shared_file_is_split_by_nick(self):
        self._write_log("bot.log", "\n".join([
            "[10:00:00] [Alpha] вошёл",
            "[10:00:01] [Beta] вошёл",
            "[10:00:02] [Alpha] Anti-Cheat Ваш IP-адрес был заблокирован",
        ]) + "\n")
        cookie = self._login()
        _, payload, _ = self._request("/api/logs", cookie=cookie)
        bots = {b["nick"]: b for b in payload["bots"]}
        self.assertEqual(sorted(bots), ["Alpha", "Beta"])
        self.assertEqual(bots["Alpha"]["tail"][-1],
                         "[10:00:02] [Alpha] Anti-Cheat Ваш IP-адрес был заблокирован")
        self.assertEqual(len(bots["Alpha"]["tail"]), 2)
        self.assertEqual(len(bots["Beta"]["tail"]), 1)

    def test_nick_filter_and_lines_cap(self):
        self._write_log("Alpha.log", "\n".join(f"a{i}" for i in range(20)) + "\n")
        self._write_log("Beta.log", "b0\nb1\n")
        cookie = self._login()
        _, payload, _ = self._request("/api/logs?nick=alpha&lines=2", cookie=cookie)
        self.assertEqual([b["nick"] for b in payload["bots"]], ["Alpha"])
        self.assertEqual(payload["bots"][0]["tail"], ["a18", "a19"])

    def test_lines_hard_cap(self):
        self._write_log("Big.log", "\n".join(f"l{i}" for i in range(2500)) + "\n")
        cookie = self._login()
        _, payload, _ = self._request("/api/logs?lines=999999", cookie=cookie)
        self.assertEqual(len(payload["bots"][0]["tail"]), web_server.MAX_LOG_LINES)
        self.assertEqual(web_server.MAX_LOG_LINES, 2000)

    def test_stale_file_without_snapshot_is_not_connected(self):
        self._write_log("Ghost.log", "давно\n", age=web_server.LOG_FRESH_SECONDS + 60)
        # BR_LOG_DIR не задан: logs/ в корне проекта подменяем через env.
        os.environ["BR_LOG_DIR"] = os.path.join(self.tmp, "logs")
        cookie = self._login()
        _, payload, _ = self._request("/api/logs", cookie=cookie)
        self.assertIs(payload["bots"][0]["connected"], False)

    def test_snapshot_status_wins_over_file_age(self):
        self._write_log("Ghost.log", "старое\n", age=web_server.LOG_FRESH_SECONDS + 60)
        os.environ["BR_LOG_DIR"] = os.path.join(self.tmp, "logs")
        self._snapshot([{"nickname": "Ghost", "server": "RED",
                         "status": "Парковка: адрес заблокирован"}])
        cookie = self._login()
        _, payload, _ = self._request("/api/logs", cookie=cookie)
        self.assertIs(payload["bots"][0]["connected"], True)

    def test_requires_auth(self):
        status, _, _ = self._request("/api/logs")
        self.assertEqual(status, 401)


class TokenTests(PanelBase):
    def test_token_opens_read_only(self):
        os.environ["PANEL_TOKEN"] = "s3cret-value"
        with mock.patch("br_servers.get_servers", return_value=FIXTURE):
            status, _, _ = self._request(
                "/api/online", headers={"X-Panel-Token": "s3cret-value"})
            self.assertEqual(status, 200)
            status2, _, _ = self._request(
                "/api/logs", headers={"X-Panel-Token": "s3cret-value"})
        self.assertEqual(status2, 200)
        # Управление и /api/state токен не открывает.
        status, _, _ = self._request(
            "/api/state", headers={"X-Panel-Token": "s3cret-value"})
        self.assertEqual(status, 401)
        status, _, _ = self._request(
            "/api/start", "POST", {"id": 1},
            headers={"X-Panel-Token": "s3cret-value"})
        self.assertEqual(status, 401)

    def test_wrong_token_gets_401(self):
        os.environ["PANEL_TOKEN"] = "s3cret-value"
        with mock.patch("br_servers.get_servers", return_value=FIXTURE):
            for given in ("", "s3cre", "s3cret-valu", "S3CRET-VALUE", "foreign"):
                status, _, _ = self._request(
                    "/api/online", headers={"X-Panel-Token": given})
                self.assertEqual(status, 401, f"пустило с токеном {given!r}")

    def test_empty_panel_token_disables_machine_access(self):
        for value in (None, "", "   "):
            if value is None:
                os.environ.pop("PANEL_TOKEN", None)
            else:
                os.environ["PANEL_TOKEN"] = value
            with mock.patch("br_servers.get_servers", return_value=FIXTURE):
                status, _, _ = self._request(
                    "/api/online", headers={"X-Panel-Token": "s3cret-value"})
            self.assertEqual(status, 401, f"PANEL_TOKEN={value!r} пустил внутрь")

    def test_compare_is_constant_time(self):
        names = web_server.Handler._machine_token_ok.__code__.co_names
        self.assertIn("compare_digest", names,
                      "токен сравнивается не через hmac.compare_digest")

    def test_cookie_session_still_works_on_read_endpoints(self):
        cookie = self._login()
        with mock.patch("br_servers.get_servers", return_value=FIXTURE):
            status, _, _ = self._request("/api/online", cookie=cookie)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
