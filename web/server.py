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

Машинный доступ к чтению:

    Мини-приложение живёт на Vercel, печенья панели у него нет. Поэтому две
    точки только для чтения — GET /api/online и GET /api/logs — принимают
    заголовок X-Panel-Token со значением из PANEL_TOKEN. Токен сравнивается
    hmac.compare_digest и НИКОГДА не открывает ни /api/state, ни POST-команды:
    утечка значения из фронтенда не даёт управлять фермой.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from logic import web_bridge  # noqa: E402
from web.auth import Auth  # noqa: E402
from web import tg_auth  # noqa: E402
from web import license as license_check  # noqa: E402

STATIC_DIR = os.path.join(HERE, "static")
BRIDGE_TIMEOUT = 25.0

# --- чтение логов с диска ------------------------------------------------
# Панель — отдельный процесс от бота, живого объекта Bot у неё нет, поэтому
# логи берутся из файлов. Каталог задаёт BR_LOG_DIR; без неё пробуем logs/,
# затем корень проекта.
DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
# Больше сотни файлов за один запрос не отдаём: панель не должна
# перемалывать каталог с тысячей логов на каждый заход.
MAX_LOG_FILES = 100
# Хвост читаем окном с конца: файл на гигабайт не должен целиком ехать в
# память ради двухсот строк.
MAX_LOG_BYTES = 2_000_000
# Строка из bot.add_log: "[12:34:56] [Nick_Name] текст". По ней общий лог
# раскидывается по ботам.
LOG_LINE_RE = re.compile(r"^\[(\d{1,2}:\d{2}:\d{2})\]\s*\[([^\[\]]+)\]\s?(.*)$")
# Статусы, при которых бот не держит сессию. Всё остальное (в игре,
# парковка, очередь, подключение) считается живым соединением.
OFFLINE_STATUS = ("Остановлен", "Отключен", "ЗАБАНЕН", "Кикнут", "Ошибка")
# Файл, не менявшийся дольше этого, похож на лог мёртвого процесса. Нужен
# только когда снимок состояния ничего не знает про этот ник.
LOG_FRESH_SECONDS = 600.0

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

    def _machine_token_ok(self) -> bool:
        """Вход по X-Panel-Token — только для чтения.

        Мини-приложение на Vercel не имеет печенья панели: оно живёт на
        другом домене, и сессию там взять неоткуда. Токен лежит в
        PANEL_TOKEN на VPS. Сравнение — hmac.compare_digest: оно не
        выходит из цикла на первом несовпавшем байте, поэтому по времени
        ответа значение нельзя подобрать посимвольно, как вышло бы с "==".

        Пустой PANEL_TOKEN (или незаданный) выключает токенный вход. Это
        не ошибка и не «пустой пароль»: иначе заголовок без значения
        пускал бы внутрь кого угодно. Токен открывает ровно две точки —
        GET /api/online и GET /api/logs. Управление, /api/state и POST
        проверяют только сессию: утёкший из фронтенда токен не даёт
        останавливать, удалять и перенастраивать ботов.
        """
        expected = (os.environ.get("PANEL_TOKEN") or "").strip()
        if not expected:
            return False
        given = self.headers.get("X-Panel-Token") or ""
        if not given:
            return False
        return hmac.compare_digest(given.encode("utf-8"),
                                   expected.encode("utf-8"))

    def _read_authed(self) -> bool:
        """Доступ на чтение: живая сессия панели либо машинный токен."""
        return self._authed() or self._machine_token_ok()

    def _query(self) -> dict:
        raw = self.path.split("?", 1)[1] if "?" in self.path else ""
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

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
        # Машинное чтение: сессия панели ИЛИ X-Panel-Token (см.
        # _machine_token_ok). Только эти две точки токен и открывает.
        if path == "/api/online":
            if not self._read_authed():
                self._json({"error": "нужен вход"}, 401)
                return
            self._json(self._online())
            return
        if path == "/api/logs":
            if not self._read_authed():
                self._json({"error": "нужен вход"}, 401)
                return
            self._json(self._logs(self._query()))
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

        # Вход по ключу доступа. Приложение шлёт токен, который уже держит;
        # подпись проверяется тем же публичным ключом, что и в приложении.
        if path == "/api/key":
            if not self.app.allow_attempt(ip):
                self._json({"error": "слишком много попыток, подожди 5 минут"}, 429)
                return
            data = self._read_json()
            token = str(data.get("token") or "")
            payload = license_check.verify(token)
            if payload is None:
                self._json({"error": "ключ не подходит или срок истёк"}, 401)
                return
            self.app.reset_attempts(ip)
            session = self.app.auth.start_session()
            cookie = (f"rakcrmp_session={session}; Path=/; HttpOnly; "
                      f"SameSite=Strict; Max-Age=43200")
            self._json(
                {"ok": True, "code": payload.get("code", ""),
                 "expires": payload.get("exp", 0)},
                cookies=[cookie],
            )
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
            "vpn_sub_add", "settings", "ping", "switch_project",
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

        # Проекты: панель переключает их той же кнопкой, что и Telegram.
        # Список серверов выше — уже серверы активного проекта.
        projects = []
        active_project = ""
        try:
            import projects as projects_mod

            projects = projects_mod.describe()
            active_project = projects_mod.active_key()
        except Exception:
            projects = []
        return {
            "servers": servers,
            "tasks": tasks,
            "projects": projects,
            "active_project": active_project,
            "tg": bool(tg_auth.bot_token()),
            "miniapp_url": tg_auth.miniapp_url(),
            "login": self.app.auth.login,
        }

    def _state(self) -> dict:
        """Свежие данные от бота, иначе последний снимок с диска."""
        reply = web_bridge.submit("list", {}, timeout=6.0)
        if reply.get("ok"):
            result = reply.get("result") or {}
            bots = result.get("bots") or []
            vpn_reply = web_bridge.submit("vpn_status", {}, timeout=6.0)
            vpn = (vpn_reply.get("result") if vpn_reply.get("ok") else None) or {}
            return {"bots": bots, "vpn": vpn,
                    "project": result.get("project") or {},
                    "live": True, "ts": time.time()}

        try:
            with open(web_bridge.STATE_FILE, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            snapshot["live"] = False
            return snapshot
        except Exception:
            return {"bots": [], "vpn": {}, "live": False,
                    "error": "бот не отвечает и снимка нет"}

    # --- онлайн серверов ----------------------------------------------

    def _online(self) -> dict:
        """Серверы с онлайном из br_servers.

        Сеть не должна валить панель: br_servers уже держит жёсткий таймаут
        и дисковый кэш, поэтому 500 тут не бывает. Если он всё же поднял
        исключение (сети нет и кэш пуст), читаем кэш сами и помечаем ответ
        stale — мини-приложение покажет старые числа вместо пустого экрана.
        """
        import br_servers

        stale = False
        reason = ""
        raw = None
        try:
            raw = br_servers.get_servers()
        except Exception as exc:
            stale = True
            reason = str(exc) or "список серверов недоступен"

        if not raw:
            cached = br_servers._load_disk_cache()
            if cached:
                raw = cached
                stale = True
                reason = reason or "сеть недоступна, отдаю дисковый кэш"

        servers = [self._norm_server(item) for item in (raw or [])
                   if isinstance(item, dict)]
        servers = [item for item in servers if item]
        # Сначала самые населённые: панель показывает верх списка.
        servers.sort(key=lambda item: (-item["online"], item["name"]))

        out = {"ok": bool(servers), "servers": servers, "stale": stale,
               "ts": time.time()}
        if reason:
            out["reason"] = reason
        return out

    @staticmethod
    def _norm_server(item: dict) -> dict | None:
        """Привести запись API к числам, bool и #RRGGBB."""
        name = str(item.get("name") or "").strip()
        try:
            sid = int(item.get("id") or 0)
        except Exception:
            sid = 0
        if not name and not sid:
            return None
        return {
            "id": sid,
            "name": name or str(sid),
            "firstname": str(item.get("firstname") or "").strip(),
            "online": Handler._as_int(item.get("online")),
            "maxonline": Handler._as_int(item.get("maxonline")),
            "x2": str(item.get("x2") or "").strip().lower()
                  in ("1", "true", "yes", "да"),
            "color": Handler._norm_color(item.get("color")),
            "url": str(item.get("url") or "").strip(),
            "ip": str(item.get("ip") or "").strip(),
            "port": str(item.get("port") or "").strip(),
        }

    @staticmethod
    def _as_int(value) -> int:
        # online/maxonline приходят строками, иногда пустыми.
        try:
            return max(0, int(str(value).strip()))
        except Exception:
            return 0

    @staticmethod
    def _norm_color(value) -> str:
        raw = str(value or "").strip()
        if raw.lower().startswith("0x"):
            raw = raw[2:]
        raw = "".join(ch for ch in raw if ch in "0123456789abcdefABCDEF")
        if len(raw) == 8:      # AARRGGBB с сервера
            raw = raw[2:]
        return "#" + raw.upper() if len(raw) == 6 else "#FFFFFF"

    # --- логи ботов с диска -------------------------------------------

    def _logs(self, query: dict) -> dict:
        """Хвост логов ботов.

        Живого объекта Bot у панели нет, поэтому источник — файлы. Каталог
        берём из BR_LOG_DIR, иначе logs/ в корне проекта. Файл на бота
        (`Nick.log`) даёт ник из имени; общий лог, где строки помечены
        `[HH:MM:SS] [Nick] ...`, раскидывается по никам. Оба расклада
        поддерживаются, потому что владелец может писать и так, и так.
        """
        lines = self._lines_param(query)
        nick = str((query.get("nick") or [""])[0] or "").strip().lower()

        dirs, explicit = self._log_dirs()
        log_dir = next((d for d in dirs if os.path.isdir(d)), None)
        if log_dir is None:
            where = dirs[0] if explicit else ", ".join(dirs)
            return {"ok": False, "bots": [],
                    "reason": f"каталога логов нет: {where}"}

        try:
            names = sorted(os.listdir(log_dir))
        except OSError as exc:
            return {"ok": False, "bots": [],
                    "reason": f"каталог логов не читается: {exc}"}

        files = []
        for name in names:
            if not name.lower().endswith(".log"):
                continue
            full = os.path.join(log_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                files.append((full, os.path.getmtime(full)))
            except OSError:
                continue
        # Свежие файлы важнее старых, но склейку ведём от старых к новым,
        # чтобы свежие строки легли в хвост.
        files.sort(key=lambda item: item[1], reverse=True)
        files = files[:MAX_LOG_FILES]
        files.reverse()

        known = self._known_bots()
        entries: dict[str, dict] = {}
        for full, mtime in files:
            tail = self._tail_lines(full, lines)
            grouped: dict[str, list[str]] = {}
            plain: list[str] = []
            for line in tail:
                hit = LOG_LINE_RE.match(line)
                if hit:
                    grouped.setdefault(hit.group(2).strip(), []).append(line)
                else:
                    plain.append(line)
            if grouped:
                for name, chunk in grouped.items():
                    self._merge_log(entries, name, chunk, mtime, lines, known)
            elif plain:
                stem = os.path.splitext(os.path.basename(full))[0]
                self._merge_log(entries, stem, plain, mtime, lines, known)

        bots = list(entries.values())
        if nick:
            bots = [item for item in bots if nick in item["nick"].lower()]
        bots.sort(key=lambda item: item["nick"].lower())

        out = {"ok": True, "bots": bots, "log_dir": log_dir, "ts": time.time()}
        if not bots:
            out["reason"] = ("логов не нашлось" if not files
                             else "под фильтр ничего не попало")
        return out

    @staticmethod
    def _log_dirs() -> tuple[list[str], bool]:
        env = (os.environ.get("BR_LOG_DIR") or "").strip()
        if env:
            path = env if os.path.isabs(env) else os.path.join(ROOT, env)
            return [path], True
        return [os.path.join(ROOT, "logs"), ROOT], False

    @staticmethod
    def _lines_param(query: dict) -> int:
        raw = (query.get("lines") or [""])[0]
        try:
            value = int(str(raw).strip())
        except Exception:
            return DEFAULT_LOG_LINES
        return max(1, min(MAX_LOG_LINES, value))

    @staticmethod
    def _tail_lines(path: str, limit: int) -> list[str]:
        """Последние limit строк файла, читая окно с конца."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - MAX_LOG_BYTES)
                fh.seek(start)
                raw = fh.read()
        except OSError:
            return []
        lines = raw.decode("utf-8", "replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # первая строка могла обрезаться на середине
        return lines[-limit:]

    def _known_bots(self) -> dict:
        """Ники из снимка состояния: сервер и статус для карточки лога."""
        try:
            with open(web_bridge.STATE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return {}
        known = {}
        for bot in (data.get("bots") or []) if isinstance(data, dict) else []:
            if not isinstance(bot, dict):
                continue
            name = str(bot.get("nickname") or "").strip()
            if name:
                known[name.lower()] = {
                    "server": str(bot.get("server") or "").strip(),
                    "status": str(bot.get("status") or "").strip(),
                }
        return known

    @staticmethod
    def _merge_log(entries: dict, nick: str, chunk: list[str], mtime: float,
                   limit: int, known: dict) -> None:
        entry = entries.get(nick)
        if entry is None:
            info = known.get(nick.lower(), {})
            status = info.get("status", "")
            entry = {
                "nick": nick,
                "server": info.get("server", ""),
                "status": status,
                "connected": Handler._is_connected(status, mtime),
                "tail": [],
            }
            entries[nick] = entry
        entry["tail"] = (entry["tail"] + chunk)[-limit:]

    @staticmethod
    def _is_connected(status: str, mtime: float) -> bool:
        if status:
            return not status.startswith(OFFLINE_STATUS)
        return (time.time() - mtime) <= LOG_FRESH_SECONDS


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
