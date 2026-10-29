#!/usr/bin/env python3
"""RakCRMP — веб-панель управления фермой ботов.

Отдельный процесс рядом с ботом. Связь — через logic.web_bridge: панель
кладёт команды в очередь, бот их исполняет своим потоком. Ни второго
менеджера, ни второго подключения к accounts.json здесь нет.

Запуск:
    python3 -m web.server            # 127.0.0.1:8080 по умолчанию
    python3 -m web.server --host 0.0.0.0 --port 8080

Пароль генерируется при первом запуске и печатается в консоль. Показывать
его повторно нечем — на диске только хеш. `--reset-password` выпускает новый.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logic import web_bridge  # noqa: E402
from web.auth import Auth  # noqa: E402
from web import tg_auth  # noqa: E402

STATIC_DIR = os.path.join(HERE, "static")
BRIDGE_TIMEOUT = 25.0

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


class App:
    """Общее состояние сервера: авторизация плюс мост."""

    def __init__(self, auth: Auth):
        self.auth = auth
        # Разрешённые попытки входа: без этого пароль можно перебирать.
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow_attempt(self, ip: str, limit: int = 8, window: float = 300.0) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._attempts.get(ip, []) if now - t < window]
            if len(hits) >= limit:
                self._attempts[ip] = hits
                return False
            hits.append(now)
            self._attempts[ip] = hits
            return True

    def reset_attempts(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "RakCRMP"
    protocol_version = "HTTP/1.1"

    # --- утилиты ------------------------------------------------------

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _token(self) -> str | None:
        # Веб-вью Telegram не всегда хранит cookie: токен принимаем и
        # заголовком, иначе вход работает через раз.
        return (self._cookie("rakcrmp_session")
                or self.headers.get("X-Rak-Token"))

    def _authed(self) -> bool:
        return self.app.auth.check_session(self._token())

    def _json(self, payload, status: int = 200, cookies: list[str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        # Никаких выходов за пределы каталога: имя собирается из белого списка
        # символов, а '..' отсекается сразу.
        if ".." in rel or rel.startswith("/"):
            self._json({"error": "не найдено"}, 404)
            return
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            # Всё неизвестное отдаём как SPA: страница сама разберётся.
            full = os.path.join(STATIC_DIR, "index.html")
            rel = "index.html"
            if not os.path.isfile(full):
                self._json({"error": "нет статики"}, 404)
                return
        ext = os.path.splitext(rel)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # index.html не кэшируем: после обновления панели нужен свежий файл.
        if rel != "index.html":
            self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # тише в journald
        pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # --- маршруты -----------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/session":
            self._json({"authed": self._authed(),
                        "login": self.app.auth.login if self._authed() else None})
            return
        if path == "/api/state":
            if not self._authed():
                self._json({"error": "нужен вход"}, 401)
                return
            self._json(self._state())
            return
        if path == "/api/config":
            if not self._authed():
                self._json({"error": "нужен вход"}, 401)
                return
            self._json(self._config())
            return
        if path.startswith("/api/"):
            self._json({"error": "не найдено"}, 404)
            return
        self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        ip = self.client_address[0]

        if path == "/api/tg":
            if not self.app.allow_attempt(ip):
                self._json({"error": "слишком много попыток, подожди 5 минут"}, 429)
                return
            data = self._read_json()
            init_data = (str(data.get("initData") or "")
                         or self.headers.get(tg_auth.HEADER) or "")
            who = tg_auth.identify(init_data)
            if who is None:
                self._json({"error": "Telegram не подтвердил вход"}, 401)
                return
            self.app.reset_attempts(ip)
            token = self.app.auth.start_session()
            cookie = (f"rakcrmp_session={token}; Path=/; HttpOnly; "
                      f"SameSite=None; Secure; Max-Age=43200")
            self._json({"ok": True, "token": token, "user": who},
                       cookies=[cookie])
            return

        if path == "/api/login":
            if not self.app.allow_attempt(ip):
                self._json({"error": "слишком много попыток, подожди 5 минут"}, 429)
                return
            data = self._read_json()
            login = str(data.get("login") or "")
            password = str(data.get("password") or "")
            if not self.app.auth.verify(login, password):
                self._json({"error": "неверный логин или пароль"}, 401)
                return
            self.app.reset_attempts(ip)
            token = self.app.auth.start_session()
            cookie = (f"rakcrmp_session={token}; Path=/; HttpOnly; "
                      f"SameSite=Strict; Max-Age=43200")
            self._json({"ok": True}, cookies=[cookie])
            return

        if path == "/api/logout":
            self.app.auth.end_session(self._cookie("rakcrmp_session"))
            self._json({"ok": True}, cookies=[
                "rakcrmp_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"])
            return

        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "нужен вход"}, 401)
                return
            self._command(path[len("/api/"):], self._read_json())
            return

        self._json({"error": "не найдено"}, 404)

    # --- мост ---------------------------------------------------------

    def _command(self, name: str, payload: dict) -> None:
        allowed = {
            "start", "stop", "restart", "unban", "delete", "task",
            "start_many", "stop_many", "delete_many", "stats", "real_world",
            "goto", "delta_goto", "goal_many",
            "add", "add_many", "proxy_bot", "proxy_all", "proxy_many",
            "vpn_status", "vpn_refresh", "vpn_use", "vpn_ban", "vpn_unban",
            "vpn_sub_add", "settings", "ping",
        }
        if name not in allowed:
            self._json({"error": f"неизвестная команда: {name}"}, 404)
            return
        reply = web_bridge.submit(name, payload, timeout=BRIDGE_TIMEOUT)
        if not reply.get("ok"):
            self._json({"error": reply.get("error") or "бот не ответил"}, 502)
            return
        self._json({"ok": True, "result": reply.get("result")})

    def _config(self) -> dict:
        """Что нужно Mini App для отрисовки форм: серверы, задачи, ссылка."""
        servers = []
        try:
            from br_servers import get_servers
            for item in get_servers() or []:
                name = str(item.get("name") or item.get("id") or "")
                if name:
                    servers.append(name)
        except Exception:
            servers = []
        try:
            from logic.settings import KNOWN_TASKS
            tasks = list(KNOWN_TASKS)
        except Exception:
            tasks = ["lvl", "quest", "mine", "factory", "bus", "farm", "stop"]
        return {
            "servers": servers,
            "tasks": tasks,
            "tg": bool(tg_auth.bot_token()),
            "miniapp_url": tg_auth.miniapp_url(),
            "login": self.app.auth.login,
        }

    def _state(self) -> dict:
        """Свежие данные от бота, иначе последний снимок с диска."""
        reply = web_bridge.submit("list", {}, timeout=6.0)
        if reply.get("ok"):
            bots = (reply.get("result") or {}).get("bots") or []
            vpn_reply = web_bridge.submit("vpn_status", {}, timeout=6.0)
            vpn = (vpn_reply.get("result") if vpn_reply.get("ok") else None) or {}
            return {"bots": bots, "vpn": vpn, "live": True, "ts": time.time()}

        try:
            with open(web_bridge.STATE_FILE, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            snapshot["live"] = False
            return snapshot
        except Exception:
            return {"bots": [], "vpn": {}, "live": False,
                    "error": "бот не отвечает и снимка нет"}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _lan_ip() -> str:
    """Адрес, по которому машину видно в локальной сети."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _print_credentials(login: str, password: str, host: str, port: int) -> None:
    line = "=" * 56
    print(f"\n{line}")
    print("  RakCRMP — панель управления")
    print(line)
    print(f"  Адрес:  http://{host}:{port}")
    print(f"  Логин:  {login}")
    print(f"  Пароль: {password}")
    print(line)
    print("  Пароль показан ОДИН раз. На диске только хеш.")
    print("  Забудешь — python3 -m web.server --reset-password")
    print(f"{line}\n", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="RakCRMP web panel")
    parser.add_argument("--host", default=os.environ.get("BR_WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("BR_WEB_PORT", "8080")))
    parser.add_argument("--reset-password", action="store_true",
                        help="выпустить новый пароль и выйти")
    parser.add_argument("--login", default=None,
                        help="логин при выпуске учётки")
    args = parser.parse_args()

    auth = Auth()

    if args.reset_password:
        login, password = auth.issue(args.login)
        _print_credentials(login, password, args.host, args.port)
        return 0

    if not auth.configured:
        login, password = auth.issue(args.login)
        _print_credentials(login, password, args.host, args.port)
    else:
        print(f"[*] Учётка уже есть, логин: {auth.login}", flush=True)
        print("[*] Пароль знает только программа. Забыл — "
              "--reset-password", flush=True)

    server = PanelServer((args.host, args.port), Handler)
    server.app = App(auth)  # type: ignore[attr-defined]

    shown = "этот компьютер" if args.host in ("127.0.0.1", "localhost") else args.host
    print(f"[+] Панель слушает {args.host}:{args.port} ({shown})", flush=True)
    if args.host in ("0.0.0.0", ""):
        print(f"[+] По сети: http://{_lan_ip()}:{args.port}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Панель остановлена")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
