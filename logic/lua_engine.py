"""Lua-скрипты для бота: отдельный процесс lua54, общение JSON-строками.

Зачем процесс, а не lupa
------------------------
`lupa` на Termux не собирается (нет колёс под android/arm64, сборка из
исходников виснет). Поэтому Lua живёт ОТДЕЛЬНЫМ процессом, а не внутри
Питона. Это не костыль, а то, что и нужно боту:

    * падение Lua не роняет бота — мы видим закрытый поток и понятную
      ошибку, аккаунт продолжает жить;
    * зависший скрипт убивается SIGKILL по конкретному PID (kill по
      шаблону имени тут запрещён — так уже дважды убивали свою оболочку);
    * у скрипта нет доступа к памяти и объектам процесса бота. Всё, что он
      может, — попросить действия через `br.*`, а решает Питон.

Мост
----
Одна строка JSON в каждую сторону. Питон пишет запрос в stdin и читает
ответ из stdout; поток разбирает один поток-читатель, поэтому таймаут на
вызов делается через очередь, а не через возню с select.

    Python -> Lua   {"id":N,"op":"load", ...}   загрузка скрипта и on_load
                    {"id":N,"op":"event","name":"on_chat","args":{...},
                     "snapshot":{"pos":..,"now":..,"tail":[...]}}
    Lua -> Python   {"op":"ready","lua":"Lua 5.4"}   один раз при старте
                    {"id":N,"ok":true,"logs":[...],"actions":[...],"dropped":0}
                    {"id":N,"ok":false,"error":"..."}

Два вида API — и почему именно так
----------------------------------
1. **Мгновенное** (br.log, br.pos, br.now, br.server_tail). Считается в
   момент вызова из СНИМКА, который приложен к запросу: позиция бота,
   монотонное время, хвост лога. Обратных запросов во время вызова нет —
   значит, нет и дедлока «оба ждут друг друга».

2. **Отложенное** (br.walk, br.tp, br.pickup, br.send_rpc, br.send_json,
   br.sleep). Скрипт только СОБИРАЕТ действия; они уезжают в ответе и
   исполняются Питоном после вызова, строго по порядку. Так скрипт не
   может ни отправить пакет сам, ни подвесить бота в середине вызова:
   всё, что меняет мир, проходит через один узкий шлюз `execute()`.

Предохранитель `armed`
----------------------
Без `armed=True` действия из MUTATING_ACTIONS не исполняются, а пишутся в
лог («холостой ход»). Это принятая в проекте схема (logic/factory.py,
logic/ground_pickup.py): сначала прогон в пустоту, потом бой. `br.armed`
скрипт видит и может вести себя скромнее, но решает всё равно Питон.

Пределы
-------
    * call_timeout      — не ответил за N секунд: процесс убит, скрипт
                          помечен сломанным (LuaTimeout);
    * max_response_bytes — строка ответа длиннее: то же самое;
    * max_actions        — действий за один такт не больше N; лишние
                          отбрасываются в Lua и считаются в `dropped`;
    * размер скрипта     — больше 512 КиБ не грузится.
Память скрипта не ограничена: на Android честного rlimit нет, и врать в
докстринге не буду. Всё остальное — время, размер ответа, число действий —
ограничено.
"""

from __future__ import annotations

import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

__all__ = [
    "LuaUnavailable", "LuaScriptError", "LuaTimeout", "LuaEngine", "LuaScript",
    "find_lua", "describe", "LUA_BIN", "LUA_HINT", "MUTATING_ACTIONS",
]

# --- где искать интерпретатор ----------------------------------------------

LUA_CANDIDATES = ("lua54", "lua5.4", "lua")
LUA_HINT = ("Lua не найден. Поставь интерпретатор: `pkg install lua54` "
            "(Termux) или `apt install lua5.4`. Можно указать свой бинарь "
            "переменной BR_LUA_BIN.")
HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "lua", "harness.lua")

# --- пределы по умолчанию ---------------------------------------------------

DEFAULT_CALL_TIMEOUT = 2.0      # секунд на один вызов скрипта
DEFAULT_LOAD_TIMEOUT = 5.0      # компиляция + тело скрипта
DEFAULT_MAX_RESPONSE_BYTES = 65536
DEFAULT_MAX_ACTIONS = 32        # действий за один такт
MAX_SLEEP_SECONDS = 3600.0
TAIL_LINES = 40                 # сколько строк лога уезжает скрипту
TAIL_LINE_CHARS = 500
TEXT_CHARS = 4000               # обрезка строк в событиях
HEX_BYTES = 2048                # сколько байт пакета отдаём как hex
TRANSCRIPT_LINES = 200          # сколько строк Lua-логов держим в памяти

# Действия, меняющие мир. Без armed=True не исполняются, а логируются.
MUTATING_ACTIONS = frozenset({"walk", "drive", "tp", "pickup", "send_rpc", "send_packet", "send_json", "chat", "keys", "vehicle", "spawn_select", "dialog", "solve_captcha", "stop_movement", "immortality", "route_save"})

# Дальше цели при ходьбе считаем дошедшими.
WALK_COMPLETION_DISTANCE = 0.5


class LuaUnavailable(RuntimeError):
    """Интерпретатора Lua нет в системе."""


class LuaScriptError(RuntimeError):
    """Скрипт не загрузился, упал или движок сломан."""


class LuaTimeout(LuaScriptError):
    """Скрипт не ответил за отведённое время — процесс убит."""


def find_lua(candidates=LUA_CANDIDATES, env=None, extra_dirs=None):
    """Путь к интерпретатору Lua или None.

    Порядок: BR_LUA_BIN -> PATH -> типовые каталоги. `env` и `extra_dirs`
    вынесены в аргументы, чтобы тест мог проверить поведение без Lua.
    """
    env = os.environ if env is None else env
    override = (env.get("BR_LUA_BIN") or "").strip()
    if override:
        if os.path.isabs(override) and os.path.exists(override):
            return override
        found = shutil.which(override)
        if found:
            return found
        return None

    for name in candidates:
        found = shutil.which(name)
        if found:
            return found

    prefix = env.get("PREFIX") or os.environ.get("PREFIX") or ""
    if extra_dirs is None:
        extra_dirs = [os.path.join(prefix, "bin") if prefix else "",
                      "/usr/bin", "/usr/local/bin"]
    for directory in extra_dirs:
        if not directory:
            continue
        for name in candidates:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


LUA_BIN = find_lua()


# --- мелочи для событий -----------------------------------------------------

def _hex(data, limit=HEX_BYTES):
    """bytes -> hex. Длинное обрезаем: скрипту весь пакет не нужен."""
    if data is None:
        return ""
    if isinstance(data, str):
        raw = data.encode("utf-8", "replace")
    else:
        try:
            raw = bytes(data)
        except (TypeError, ValueError):
            return ""
    cut = raw[:limit]
    text = cut.hex()
    if len(raw) > limit:
        text += f"…(+{len(raw) - limit} Б)"
    return text


def _json_safe(value, depth=0):
    """Привести аргументы события к тому, что переживает json.dumps.

    Пакеты приходят bytes, интерфейсы — вложенными словарями; и то и другое
    должно доехать до Lua без исключений и без мегабайтов.
    """
    if depth > 6:
        return "…"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:TEXT_CHARS]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _hex(value)
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 64:
                out["…"] = f"ещё {len(value) - index}"
                break
            out[str(key)[:128]] = _json_safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [_json_safe(item, depth + 1) for item in items[:64]]
        if len(items) > 64:
            out.append(f"…ещё {len(items) - 64}")
        return out
    return str(value)[:TEXT_CHARS]


def describe() -> str:
    """Строка для --help: что за движок и какие у него пределы."""
    found = LUA_BIN or "не найден"
    return (f"Lua-скрипты отдельным процессом ({found}): вызов "
            f"{DEFAULT_CALL_TIMEOUT:.0f} с, ответ до "
            f"{DEFAULT_MAX_RESPONSE_BYTES // 1024} КиБ, "
            f"{DEFAULT_MAX_ACTIONS} действий за такт; без --armed мир не меняется")


# --- движок -----------------------------------------------------------------

class LuaEngine:
    """Процесс lua54 с харнессом и разбором строкового протокола.

    Один процесс — один скрипт: так проще и безопаснее, а цена — мегабайт
    памяти на интерпретатор.
    """

    def __init__(
        self,
        *,
        lua_bin=None,
        harness=None,
        call_timeout=DEFAULT_CALL_TIMEOUT,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        max_actions=DEFAULT_MAX_ACTIONS,
        log=None,
        stderr_lines=25,
    ):
        self.lua_bin = lua_bin or find_lua()
        if not self.lua_bin:
            raise LuaUnavailable(LUA_HINT)
        self.harness = harness or HARNESS_PATH
        self.call_timeout = float(call_timeout)
        self.max_response_bytes = int(max_response_bytes)
        self.max_actions = int(max_actions)
        self.log = log or (lambda _message: None)

        self._proc = None
        self._queue = None
        self._threads = []
        self._stderr = deque(maxlen=stderr_lines)
        self._id = 0
        self.broken = False
        self.broken_reason = ""
        self.lua_version = ""

    # --- жизненный цикл ---

    def start(self, script_path=None):
        if self._proc is not None:
            return self
        if not os.path.exists(self.harness):
            raise LuaScriptError(f"нет загрузчика Lua: {self.harness}")
        command = [self.lua_bin, self.harness]
        if script_path:
            command.append(str(script_path))
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except OSError as exc:
            raise LuaUnavailable(f"не запустил {self.lua_bin}: {exc}. {LUA_HINT}") from exc

        self._queue = queue.Queue()
        self._spawn(self._pump_stdout)
        self._spawn(self._pump_stderr)
        try:
            raw = self._take(self.call_timeout)
        except LuaTimeout:
            self._kill()
            raise LuaScriptError(
                f"Lua не сказал ready за {self.call_timeout:g} с. "
                f"stderr: {self.stderr_tail() or '—'}")
        try:
            first = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            first = raw
        if not isinstance(first, dict) or first.get("op") != "ready":
            self._kill()
            raise LuaScriptError(
                f"странный первый ответ Lua: {first!r}. stderr: "
                f"{self.stderr_tail() or '—'}")
        self.lua_version = str(first.get("lua") or "")
        return self

    def _spawn(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _pump_stdout(self):
        stream = self._proc.stdout
        failure = None
        try:
            while True:
                # Читаем не больше предела+1: строка длиннее — уже нарушение.
                line = stream.readline(self.max_response_bytes + 1)
                if not line:
                    break
                self._queue.put(line)
        except Exception as exc:                      # поток закрылся под нами
            failure = exc
        if failure is not None:
            self._queue.put(failure)
        self._queue.put(None)

    def _pump_stderr(self):
        stream = self._proc.stderr
        try:
            while True:
                line = stream.readline(2048)
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        except Exception:
            pass

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    @property
    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def stderr_tail(self):
        return " | ".join(self._stderr)

    # --- протокол ---

    def _take(self, timeout):
        if self._queue is None:
            raise LuaScriptError("движок не запущен")
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise LuaTimeout(
                f"Lua молчит дольше {timeout:g} с (скрипт завис в бесконечном "
                f"цикле?). stderr: {self.stderr_tail() or '—'}")
        if item is None:
            raise LuaScriptError(
                f"Lua закрыл stdout (процесс умер?). stderr: "
                f"{self.stderr_tail() or '—'}")
        if isinstance(item, Exception):
            raise LuaScriptError(f"чтение из Lua упало: {item!r}")
        return item

    def request(self, payload, *, timeout=None):
        """Отправить запрос, дождаться ответа-словаря. Таймаут убивает процесс."""
        if self.broken:
            raise LuaScriptError(f"движок сломан: {self.broken_reason}")
        if not self.alive:
            raise LuaScriptError("Lua-процесс не запущен или уже мёртв")

        timeout = self.call_timeout if timeout is None else float(timeout)
        self._id += 1
        request_id = self._id
        body = dict(payload)
        body["id"] = request_id
        try:
            line = json.dumps(body, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise LuaScriptError(f"запрос не переводится в JSON: {exc!r}") from exc

        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._mark_broken(f"не смог записать в Lua: {exc!r}")
            raise LuaScriptError(f"Lua не принимает запросы: {exc!r}") from exc

        deadline = timeout
        while True:
            try:
                raw = self._take(deadline)
            except LuaTimeout:
                self._kill()
                self._mark_broken(f"таймаут {timeout:g} с на вызове")
                raise
            break

        if len(raw) > self.max_response_bytes:
            self._kill()
            self._mark_broken(
                f"ответ больше {self.max_response_bytes} байт ({len(raw)})")
            raise LuaScriptError(self.broken_reason)
        if not raw.endswith(b"\n"):
            self._kill()
            self._mark_broken("ответ Lua без перевода строки (поток разъехался)")
            raise LuaScriptError(self.broken_reason)

        try:
            answer = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._kill()
            self._mark_broken(f"ответ не разобрался как JSON: {exc!r}")
            raise LuaScriptError(self.broken_reason) from exc

        if not isinstance(answer, dict) or answer.get("id") != request_id:
            self._kill()
            self._mark_broken(f"ответ не на наш запрос: {answer!r}")
            raise LuaScriptError(self.broken_reason)
        return answer

    def ping(self, timeout=1.0):
        return self.request({"op": "ping"}, timeout=timeout).get("pong") is True

    # --- остановка ---

    def _mark_broken(self, reason):
        self.broken = True
        if not self.broken_reason:
            self.broken_reason = reason
        self.log(f"[lua] движок сломан: {reason}")

    def _kill(self):
        """SIGKILL по конкретному PID. Никаких kill по шаблону."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def close(self, *, force=False):
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None and not force and not self.broken:
            try:
                self._id += 1
                body = json.dumps({"id": self._id, "op": "shutdown"}).encode() + b"\n"
                proc.stdin.write(body)
                proc.stdin.flush()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._kill()
        for stream in ("stdin", "stdout", "stderr"):
            handle = getattr(proc, stream, None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        self._proc = None


# --- скрипт -----------------------------------------------------------------

class LuaScript:
    """Один .lua-скрипт: загрузка, события, исполнение его действий.

    Скрипт ничего не отправляет сам. Он возвращает список действий, а
    `execute()` — единственное место, где действие превращается в вызов
    `bot.api`. Поэтому предохранитель `armed` стоит ровно в одном месте и
    его нельзя обойти из Lua.
    """

    def __init__(
        self,
        path,
        *,
        api=None,
        log=None,
        armed=False,
        clock=time.monotonic,
        sleep=time.sleep,
        server_tail=None,
        engine=None,
        call_timeout=DEFAULT_CALL_TIMEOUT,
        max_actions=DEFAULT_MAX_ACTIONS,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
        seed=None,
        tick_interval=0.5,
        bot=None,
        mode="addon",
        schedule=None,
        should_stop=None,
        captcha_handler=None,
        route_handler=None,
    ):
        self.captcha_handler = captcha_handler
        self.route_handler = route_handler
        self.route_status = {}
        self.should_stop = should_stop or (lambda: False)
        self.bot = bot
        self.mode = mode
        self.schedule = schedule
        self.path = os.path.abspath(str(path))
        self.api = api
        self.log = log or print
        self.armed = bool(armed)
        self.clock = clock
        self.sleep = sleep
        self.server_tail = server_tail
        self.seed = seed
        self.tick_interval = float(tick_interval)
        self.engine = engine or LuaEngine(
            call_timeout=call_timeout,
            max_actions=max_actions,
            max_response_bytes=max_response_bytes,
            log=self.log,
        )
        self.loaded = False
        self.failed = ""
        self.transcript = deque(maxlen=TRANSCRIPT_LINES)
        self.stats = {"calls": 0, "actions": 0, "sent": 0, "skipped": 0,
                      "errors": 0, "dropped": 0}
        self._load_timeout = max(float(call_timeout), DEFAULT_LOAD_TIMEOUT) * 2
        self.movement_id = None
        self.initial_actions = []

    # --- состояние ---

    @property
    def broken(self) -> bool:
        """Скрипт больше не обслуживается: упал, завис или превысил пределы."""
        return bool(self.failed) or self.engine.broken

    @property
    def broken_reason(self) -> str:
        return self.failed or self.engine.broken_reason

    def _say(self, message):
        text = f"[lua {os.path.basename(self.path)}] {message}"
        self.transcript.append(text)
        try:
            self.log(text)
        except Exception:
            pass

    def _die(self, message):
        self.failed = message
        self._say(f"скрипт помечен сломанным: {message}")

    # --- снимок для br.* ---

    def snapshot(self):
        """То, что скрипт видит как «сейчас»: позиция, время, хвост лога."""
        point = None
        if self.api is not None:
            try:
                raw = self.api.getbotposition()
            except Exception:
                raw = None
            if raw and len(raw) >= 3:
                try:
                    point = {"x": float(raw[0]), "y": float(raw[1]), "z": float(raw[2])}
                except (TypeError, ValueError):
                    point = None
        tail = []
        if self.server_tail is not None:
            try:
                tail = [str(line)[:TAIL_LINE_CHARS]
                        for line in list(self.server_tail() or [])][-TAIL_LINES:]
            except Exception:
                tail = []
        session = getattr(self.api, "session", None)
        connected = bool(session and getattr(session, "connected", False))
        vehicle_id = health = money = interior = None
        if self.api is not None:
            try:
                player = self.api.session.players.bot
                vehicle_id = int(player.get_current_vehicle_id())
            except Exception:
                pass
            for name in ("health", "money", "interior"):
                try:
                    value = getattr(self.api, "getbot" + name)()
                    if isinstance(value, (int, float)):
                        if name == "health": health = value
                        elif name == "money": money = value
                        else: interior = value
                except Exception:
                    pass
        from logic.lua_settings import load_settings
        settings = load_settings(self.path)
        return {"settings": settings, "route": self.route_status, "pos": point, "now": float(self.clock()), "tail": tail,
                "connected": connected, "vehicle_id": vehicle_id,
                "health": health, "money": money, "interior": interior,
                "bot_id": getattr(self.bot, "bot_id", None), "mode": self.mode}

    # --- загрузка ---

    def load(self, *, execute_actions=True):
        if self.failed:
            raise LuaScriptError(self.failed)
        if self.loaded:
            return self
        if not os.path.exists(self.path):
            self._die(f"нет файла скрипта: {self.path}")
            raise LuaScriptError(self.failed)

        self.engine.start(self.path)
        seed = self.seed
        if seed is None:
            seed = random.randrange(1, 2 ** 31)
        answer = self.engine.request(
            {"op": "load", "path": self.path, "armed": self.armed,
             "seed": int(seed), "max_actions": self.engine.max_actions,
             "snapshot": self.snapshot()},
            timeout=self._load_timeout,
        )
        self._absorb(answer)
        if answer.get("ok") is False:
            self._die(f"скрипт не загрузился: {answer.get('error')}")
            raise LuaScriptError(self.failed)
        self.loaded = True
        self._say(f"загружен ({self.engine.lua_version or 'lua'}), "
                  f"режим {'БОЕВОЙ' if self.armed else 'ХОЛОСТОЙ'}, seed={seed}")
        # A caller can defer these actions to its worker instead of blocking
        # the console during br.sleep or a long-running movement in on_load.
        self.initial_actions = answer.get("actions") or []
        if execute_actions:
            self.execute_all(self.initial_actions)
            self.initial_actions = []
        return self

    def _absorb(self, answer):
        """Логи и действия из ответа: логируем, действия исполняем."""
        for line in answer.get("logs") or []:
            self._say(str(line))
        dropped = int(answer.get("dropped") or 0)
        if dropped:
            self.stats["dropped"] += dropped
            self._say(f"отброшено действий сверх лимита: {dropped} "
                      f"(лимит {self.engine.max_actions} за такт)")

    # --- вызовы ---

    def feed(self, name, args=None, *, timeout=None):
        """Отдать скрипту событие `name` и исполнить то, что он попросил.

        Возвращает список результатов по действиям. Ошибка внутри Lua
        поднимает LuaScriptError, но движок остаётся живым: следующий вызов
        снова сработает. Так один кривой пакет не убивает прогон.
        """
        if self.failed:
            raise LuaScriptError(self.failed)
        if not self.loaded:
            raise LuaScriptError("скрипт ещё не загружен: сначала load()")

        payload = {
            "op": "event",
            "name": str(name),
            "args": _json_safe(args if args is not None else {}),
            "snapshot": self.snapshot(),
        }
        self.stats["calls"] += 1
        answer = self.engine.request(payload, timeout=timeout)
        self._absorb(answer)

        if answer.get("ok") is False:
            self.stats["errors"] += 1
            error = str(answer.get("error") or "неизвестная ошибка Lua")
            self._say(f"ошибка в обработчике {name}: {error}")
            raise LuaScriptError(error)

        return self.execute_all(answer.get("actions") or [])

    def tick(self, *, timeout=None):
        """Один такт: on_tick. Плюс действия, которые он попросил."""
        return self.feed("on_tick", {}, timeout=timeout)

    def execute_all(self, actions):
        results = []
        for action in actions:
            if self.should_stop():
                break
            results.append(self.execute(action))
        return results

    # --- исполнение действий ---

    def execute(self, action):
        """Единственный шлюз между Lua и миром бота."""
        if not isinstance(action, dict):
            self.stats["errors"] += 1
            self._say(f"действие не таблица: {action!r}")
            return {"kind": "", "status": "error", "detail": "не таблица"}
        kind = str(action.get("kind") or "")
        self.stats["actions"] += 1

        if kind in MUTATING_ACTIONS and not self.armed:
            detail = f"холостой ход: {kind} {self._describe(action)} не отправлен"
            self.stats["skipped"] += 1
            self._say(detail)
            return {"kind": kind, "status": "skipped", "detail": detail}

        try:
            if kind in MUTATING_ACTIONS and kind != "route_save":
                session = getattr(self.api, "session", None)
                if not session or not getattr(session, "connected", False):
                    raise LuaScriptError("нет подключения к серверу")
            if kind == "walk":
                self.movement_id = self._api("MoveToCoord", float(action["x"]), float(action["y"]),
                          float(action["z"]), mode="walk",
                          completion_distance=WALK_COMPLETION_DISTANCE)
            elif kind == "drive":
                self.movement_id = self._api("MoveToCoord", float(action["x"]), float(action["y"]),
                          float(action["z"]), mode="vehicle", completion_distance=1.0)
            elif kind == "stop_movement":
                self._api("StopMoveToCoord")
            elif kind == "immortality":
                from logic import immortal
                if self.bot is None: raise LuaScriptError("нет бота")
                if action.get("enabled"): immortal.enable(self.bot, log=self.log)
                else: immortal.stop(self.bot, log=self.log)
            elif kind == "chat":
                self._api("sendchat", str(action.get("text") or ""))
            elif kind == "keys":
                self._api("SetKey", keys=int(action.get("keys") or 0),
                          lr=int(action.get("lr") or 0), ud=int(action.get("ud") or 0))
            elif kind == "vehicle":
                self._api("setbotvehicle", int(action.get("id") or 0), seat_id=int(action.get("seat") or 0))
            elif kind == "spawn_select":
                if self.bot is None: raise LuaScriptError("нет бота для выбора спавна")
                from logic.spawn import select_spawn
                if not select_spawn(self.bot, int(action["index"]), delay=0):
                    raise LuaScriptError("выбор спавна не отправлен")
            elif kind == "dialog":
                session = getattr(self.api, "session", None)
                if not session: raise LuaScriptError("нет сессии для диалога")
                if not session.send_dialog_response(int(action["id"]), int(action.get("response", 1)),
                                                    int(action.get("item", 0)), str(action.get("text") or "")):
                    raise LuaScriptError("ответ на диалог не отправлен")
            elif kind == "solve_captcha":
                if self.captcha_handler is None:
                    raise LuaScriptError("решатель капчи не подключён")
                if not self.captcha_handler(action.get("data")):
                    raise LuaScriptError("капча отсутствует или уже обрабатывается")
            elif kind in ("route_start", "route_pause", "route_resume", "route_stop", "route_save"):
                if self.route_handler is None: raise LuaScriptError("маршруты не подключены")
                self.route_handler(action)
            elif kind == "after":
                if self.schedule is None: raise LuaScriptError("таймеры не подключены")
                self.schedule(float(action["sec"]), str(action["name"]), action.get("data") or {})
            elif kind == "tp":
                self._api("DeltaTeleportTo", float(action["x"]), float(action["y"]),
                          float(action["z"]), abort_check=lambda: bool(self.sleep(0)))
            elif kind == "pickup":
                pickup_id = action.get("id")
                if pickup_id is None:
                    self._api("pickup_ground")
                else:
                    self._api("pickup_interact", int(pickup_id))
            elif kind == "send_rpc":
                payload = bytes.fromhex(str(action.get("payload_hex") or ""))
                rpc_id = int(action["rpc_id"])
                if not 0 <= rpc_id <= 511: raise ValueError("rpc_id вне диапазона 0..511")
                self._api("SendRPC", rpc_id, payload)
            elif kind == "send_packet":
                payload = bytes.fromhex(str(action.get("payload_hex") or ""))
                packet_id = int(action["packet_id"])
                if not 0 <= packet_id <= 255: raise ValueError("packet_id вне диапазона 0..255")
                self._api("SendPacket", packet_id, payload)
            elif kind == "send_json":
                self._send_json(int(action["interface_id"]), action.get("payload"),
                                str(action.get("packet_name") or "interface_sync"))
            elif kind == "sleep":
                seconds = min(max(float(action.get("sec") or 0.0), 0.0), MAX_SLEEP_SECONDS)
                self.sleep(seconds)
            else:
                raise LuaScriptError(f"неизвестное действие: {kind!r}")
        except Exception as exc:
            self.stats["errors"] += 1
            detail = f"{kind} {self._describe(action)} упал: {exc!r}"
            self._say(detail)
            return {"kind": kind, "status": "error", "detail": detail}

        self.stats["sent"] += 1
        return {"kind": kind, "status": "sent", "detail": self._describe(action)}

    def _api(self, name, *args, **kwargs):
        if self.api is None:
            raise LuaScriptError(f"действие {name}: api не подключён")
        method = getattr(self.api, name, None)
        if method is None:
            raise LuaScriptError(f"действие {name}: у api нет метода {name}")
        result = method(*args, **kwargs)
        if result is False:
            raise LuaScriptError(f"{name}: действие не принято")
        return result

    def _send_json(self, interface_id, payload, packet_name="interface_sync"):
        from samp.sender import PACKET_DELIVERY
        if packet_name not in PACKET_DELIVERY or not packet_name.startswith("interface_"):
            raise LuaScriptError("неизвестный тип доставки интерфейса")
        session = getattr(self.api, "session", None)
        sender = getattr(session, "send_json", None)
        if sender is None:
            raise LuaScriptError("действие send_json: у сессии нет send_json")
        result = sender(interface_id, payload, packet_name=packet_name)
        if result is False:
            raise LuaScriptError("send_json: сессия не подключена или отправка не удалась")
        return result

    @staticmethod
    def _describe(action):
        kind = str(action.get("kind") or "")
        if kind in ("walk", "drive", "tp"):
            def display(value):
                try: return f"{float(value):.1f}"
                except (ValueError, TypeError): return repr(value)[:30]
            return "(" + ", ".join(display(action.get(k)) for k in ("x", "y", "z")) + ")"
        if kind == "pickup":
            return "с земли" if action.get("id") is None else f"pickup id={action.get('id')}"
        if kind == "send_rpc":
            return f"rpc {action.get('rpc_id')} (данные скрыты)"
        if kind == "chat":
            return "текст скрыт"
        if kind == "send_json":
            return f"интерфейс {action.get('interface_id')}"
        if kind == "sleep":
            try: return f"{float(action.get('sec') or 0):.2f} с"
            except (ValueError, TypeError): return repr(action.get('sec'))[:30]
        return repr(action)[:120]

    # --- подписка на события бота ---

    MONITORED_EVENTS = (
        "onReceiveJSON", "onReceiveRPC", "onShowChat", "onNpcDialog",
        "onReceivePacket", "onSpawn", "onconnect", "disconnect",
    )

    def bind(self, emitter):
        """Подписать скрипт на события. Только subscribe: on() — декоратор
        и падает на связанном методе (AttributeError: 'method' object has
        no attribute '_event_sync'). sync=True обязателен: скрипт меняет своё
        состояние и должен идти в потоке шины, а не в пуле воркеров."""
        for event in self.MONITORED_EVENTS:
            handler = getattr(self, self._handler_name(event), None)
            if handler is not None:
                emitter.subscribe(event, handler, sync=True)
        return self

    def unbind(self, emitter):
        for event in self.MONITORED_EVENTS:
            handler = getattr(self, self._handler_name(event), None)
            if handler is not None:
                try:
                    emitter.off(event, handler)
                except Exception:
                    pass
        return self

    @staticmethod
    def _handler_name(event):
        return {
            "onReceiveJSON": "on_receive_json",
            "onReceiveRPC": "on_receive_rpc",
            "onShowChat": "on_show_chat",
            "onNpcDialog": "on_npc_dialog",
            "onReceivePacket": "on_receive_packet",
            "onSpawn": "on_spawn",
            "onconnect": "on_connect",
            "disconnect": "on_disconnect",
        }[event]

    def _safe_feed(self, name, args):
        """Обёртка для шины: ошибка Lua не должна лететь в emitter."""
        try:
            self.feed(name, args)
        except LuaScriptError as exc:
            self._say(f"событие {name} не обработано: {exc}")
        except Exception as exc:
            self._say(f"событие {name} упало неожиданно: {exc!r}")

    def on_receive_json(self, bot, interface_id, data):
        self._safe_feed("on_json", {"interface_id": _json_safe(interface_id),
                                    "data": _json_safe(data)})

    def on_receive_rpc(self, bot, rpc_id, payload):
        self._safe_feed("on_rpc", {"rpc_id": _json_safe(rpc_id),
                                   "payload_hex": _hex(payload),
                                   "size": len(payload) if payload else 0})

    def on_show_chat(self, bot, message):
        self._safe_feed("on_chat", {"message": _json_safe(message)})

    def on_npc_dialog(self, bot, name, text, model, buttons):
        self._safe_feed("on_dialog", {"npc": _json_safe(name),
                                      "text": _json_safe(text),
                                      "model": _json_safe(model),
                                      "buttons": _json_safe(buttons)})

    def on_receive_packet(self, bot, packet_id, body):
        self._safe_feed("on_packet", {"packet_id": _json_safe(packet_id),
                                      "body_hex": _hex(body),
                                      "size": len(body) if body else 0})

    def on_spawn(self, bot):
        self._safe_feed("on_spawn", {})

    def on_connect(self, bot):
        self._safe_feed("on_connect", {})

    def on_disconnect(self, bot):
        self._safe_feed("on_disconnect", {})

    # --- остановка ---

    def close(self, *, force=False):
        if self.movement_id is not None and self.api is not None:
            try:
                self.api.StopMoveToCoord(self.movement_id, reason="lua_stopped")
            except Exception:
                pass
            self.movement_id = None
        try:
            self.engine.close(force=force)
        except Exception as exc:
            self._say(f"остановка движка: {exc!r}")

    def report(self):
        data = dict(self.stats)
        data.update({
            "script": os.path.basename(self.path),
            "armed": self.armed,
            "loaded": self.loaded,
            "broken": self.broken,
            "reason": self.broken_reason,
            "lua": self.engine.lua_version,
        })
        return data
