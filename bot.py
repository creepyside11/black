#!/usr/bin/env python3
# Engine: SA-MP / Black Russia RakNet | Python 3 | Termux
# Build: pip install -r requirements.txt (if any)
# Run: python3 bot.py [сервер/номер] [никнейм] [пароль]

import sys
import os
import time
import random
import threading
import json
import resource
import re
from collections import deque

# Оптимизация системы для поддержки до 300+ ботов одновременно в Termux / Linux
# 128 KB стек на поток позволяет держать 80-100 потоков всего в ~12 MB RAM
try:
    threading.stack_size(128 * 1024)
except Exception:
    pass

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 8192), max(hard, 8192)))
except Exception:
    pass

import br_servers
from core.network import Network
from core import security
from core import logger
from core.protocol import RakNetProtocol
from samp.api import BotAPI
from samp.session import SampSession
from samp.sync import PlayerSyncScheduler
from logic.registration import RegistrationConfig
from logic.json_ui import JsonUIConfig
from logic.events import emitter
from logic.bot_tasks import LevelGrinderTask, QuestMasterTask, MinerTask, FactoryTask, BusDriverTask
from tg_controller import TelegramBotController

# Файл с аккаунтами (рядом с bot.py, независимо от cwd)
ACCOUNTS_FILE = os.environ.get(
    "BR_ACCOUNTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json"),
)

PROXIES_FILE = os.environ.get(
    "BR_PROXIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt"),
)

# Пул прокси и VPN-подписок: состояние рядом с accounts.json
PROXY_POOL_FILE = os.environ.get(
    "BR_PROXY_POOL_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies_pool.json"),
)

# Как часто перепроверять узлы. 300 секунд = 5 минут, как и просили.
PROXY_REFRESH_INTERVAL = float(os.environ.get("BR_PROXY_REFRESH", "300"))

# Максимальная длина игрового ника в Black Russia
MAX_NICKNAME_LEN = 20

# Формат строки лога, привязанной к боту: "[12:34:56] [Nick_Name] текст".
# Используется для O(1) маршрутизации лога по _nick_index.
_LOG_NICK_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s+\[([^\]\s]+)\]")

# Режим экстремальной экономии ресурсов для 50-80+ ботов на слабых смартфонах
ECO_80_MODE = os.environ.get("BR_ECO_80", "1").lower() in ("1", "true", "yes", "on")

# История логов в памяти (на 80 ботов держим компактный буфер)
MAX_LOG_HISTORY = 20 if ECO_80_MODE else 60

# Тикрейт сетевого цикла одного бота:
# В ECO-режиме снижаем до 6 FPS для АФК/прокачки. При 80 ботах это всего 480 wakeups/сек на всю систему!
BOT_NETWORK_FPS = max(4.0, float(os.environ.get("BR_BOT_FPS", "6.0" if ECO_80_MODE else "12.0")))

# Сколько ботов одновременно проходят хэндшейк на один сервер и суммарно.
CONNECT_PER_SERVER = max(1, int(os.environ.get("BR_CONNECT_PER_SERVER", "2")))
CONNECT_GLOBAL = max(1, int(os.environ.get("BR_CONNECT_GLOBAL", "4")))
CONNECT_START_INTERVAL = max(0.5, float(os.environ.get("BR_CONNECT_INTERVAL", "2.5" if ECO_80_MODE else "1.8")))

# Очередь при переполненном сервере (как в лаунчере).
QUEUE_BASE_DELAY = max(2.0, float(os.environ.get("BR_QUEUE_BASE", "8")))
QUEUE_MAX_DELAY = max(QUEUE_BASE_DELAY, float(os.environ.get("BR_QUEUE_MAX", "45")))
QUEUE_JITTER = 0.35

# Пауза между реконнектами после разрыва.
RECONNECT_MIN = max(1.0, float(os.environ.get("BR_RECONNECT_MIN", "8")))
RECONNECT_MAX = max(RECONNECT_MIN, float(os.environ.get("BR_RECONNECT_MAX", "20")))

# accounts.json пишется не чаще раза в N секунд (на телефоне флеш — узкое место).
SAVE_DEBOUNCE = max(0.0, float(os.environ.get("BR_SAVE_DEBOUNCE", "10")))

# Чем сервер сообщает о переполнении.
FULL_SERVER_MARKERS = (
    "переполнен",
    "нет свободных",
    "свободных мест",
    "мест нет",
    "очеред",
    "server is full",
    "server full",
    "no free slots",
    "все слоты заняты",
)


class ConnectGate:
    """Глобальный ограничитель подключений.

    Держит не больше N одновременных хэндшейков на сервер и M суммарно,
    плюс минимальный интервал между стартами. Тридцать ботов встают
    в очередь и заходят по одному, а не лавиной — за лавину сервер кикает.
    """

    def __init__(
        self,
        per_server: int = CONNECT_PER_SERVER,
        global_limit: int = CONNECT_GLOBAL,
        interval: float = CONNECT_START_INTERVAL,
    ):
        self.per_server = max(1, int(per_server))
        self.global_limit = max(1, int(global_limit))
        self.interval = max(0.0, float(interval))

        self._cond = threading.Condition()
        self._active: dict[str, int] = {}
        self._last_start: dict[str, float] = {}
        self._global_active = 0

    def wait_turn(self, server: str, timeout: float = 180.0) -> bool:
        """Блокируется, пока не освободится слот. Возвращает False по таймауту."""
        deadline = time.monotonic() + timeout
        key = str(server or "")

        with self._cond:
            while True:
                if (
                    self._global_active < self.global_limit
                    and self._active.get(key, 0) < self.per_server
                ):
                    wait_for = (
                        self._last_start.get(key, 0.0)
                        + self.interval
                        - time.monotonic()
                    )
                    if wait_for <= 0:
                        self._active[key] = self._active.get(key, 0) + 1
                        self._global_active += 1
                        self._last_start[key] = time.monotonic()
                        return True
                    self._cond.wait(min(wait_for, 0.5))
                else:
                    self._cond.wait(0.5)

                if time.monotonic() >= deadline:
                    return False

    def release(self, server: str) -> None:
        key = str(server or "")
        with self._cond:
            self._active[key] = max(0, self._active.get(key, 0) - 1)
            self._global_active = max(0, self._global_active - 1)
            self._cond.notify_all()


class BotInstance:
    def __init__(
        self,
        bot_id: int,
        host: str,
        port: int,
        nickname: str,
        password: str,
        server_name: str = "",
        auto_regen_nick: bool = False,
        manager: any = None,
        is_banned: bool = False,
        default_task: str = "lvl",
        proxy: str = "",
    ):
        self.bot_id = bot_id
        self.host = host
        self.port = int(port)
        self.nickname = nickname
        self.password = password
        self.server_name = server_name.upper()
        self.auto_regen_nick = auto_regen_nick
        self.manager = manager
        self.stopping = False
        self.is_banned = is_banned
        self.default_task_name = default_task
        self.proxy = proxy.strip() if proxy else ""

        self.level_str = "-"
        self.money = 0
        self.target_lvl = None  # Целевой уровень (например 4)
        self._quest_helpers = None
        self.target_achieved = False
        self.last_stats_check = 0.0
        self.stats_pending = False
        self.stats_dialog_id = None

        self.logs = deque(maxlen=MAX_LOG_HISTORY)
        self.status = "ЗАБАНЕН" if self.is_banned else "Остановлен"
        self.connected_time = None
        self.thread = None
        self.active_task = None

        # Спавн: watchdog выводит бота из зоны авторизации на вокзал
        self.spawn_watchdog = None
        self.spawned_at = None
        self.spawn_point = None
        self.spawn_report = None

        self.reconnect_thread = None
        self.reconnect_cancel_event = threading.Event()
        self.scheduled_reconnect_delay = None

        # Очередь на переполненный сервер (как в лаунчере)
        self.queue_mode = False
        self.queue_attempts = 0
        self.queue_started_at = 0.0
        self.queue_reason = ""
        self._queue_timer_active = False
        self._sched_lock = threading.Lock()
        self._reconnect_pending = False
        self._sched_generation = 0
        # Подряд идущие неудачные попытки подключения
        self.connect_failures = 0

        self.session = None
        self.api = None
        self.network = None
        self.protocol = None
        self.sync_scheduler = None

    def get_numeric_level(self) -> int:
        if self.level_str and self.level_str != "-":
            m = re.search(r'(\d+)', self.level_str)
            if m:
                return int(m.group(1))
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            return int(getattr(self.session.players.bot, "score", 0))
        return 0

    def check_target_level_achieved(self):
        if self.target_lvl and not self.target_achieved:
            cur_lvl = self.get_numeric_level()
            if cur_lvl >= self.target_lvl:
                self.target_achieved = True
                self.add_log(f"🎯 ЦЕЛЬ ДОСТИГНУТА! Получен уровень {cur_lvl} (цель: {self.target_lvl} lvl). Бот останавливается.")
                if self.manager and hasattr(self.manager, "notify_users"):
                    msg = (
                        f"🎯 <b>Цель аккаунта достигнута!</b>\n\n"
                        f"Бот: <b>#{self.bot_id} [{self.nickname}]</b>\n"
                        f"Сервер: <b>{self.server_name}</b>\n"
                        f"Текущий уровень: <b>{self.level_str}</b>\n"
                        f"Целевой уровень: <b>{self.target_lvl} lvl</b>\n\n"
                        f"<i>Аккаунт успешно остановлен.</i>"
                    )
                    self.manager.notify_users(msg)
                self.stop()
                if self.manager:
                    self.manager.save_accounts()

    def get_level(self) -> str:
        # 1. Если спарсили детальный уровень с EXP через диалог /mm
        if self.level_str and self.level_str != "-":
            return self.level_str
        # 2. Иначе берем игровой score из сетевых пакетов SA-MP (scoreboard/score)
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            s = int(getattr(self.session.players.bot, "score", 0))
            if s > 0:
                self.level_str = f"{s} lvl"
                return self.level_str
        return self.level_str or "-"

    def get_balance_str(self) -> str:
        # Проверяем баланс из сессии или кэша
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            m = int(getattr(self.session.players.bot, "money", 0))
            if m > 0:
                self.money = m
        if self.money > 0:
            return f"{self.money:,} руб.".replace(",", " ")
        return "0 руб."

    @property
    def quest_helpers(self):
        """Помощники сюжетки: НПС-диалоги, ларёк, звонки, покраска.

        Создаются лениво — боту без сюжетки они не нужны.
        """
        if self._quest_helpers is None:
            from logic.quest_helpers import QuestHelpers
            self._quest_helpers = QuestHelpers(
                self, log=self.add_log
            )
        return self._quest_helpers

    def add_log(self, text: str):
        t_str = time.strftime("[%H:%M:%S]")
        formatted = f"{t_str} [{self.nickname}] {text}"
        self.logs.append(formatted)
        return formatted

    # ==================== ОЧЕРЕДЬ НА ПЕРЕПОЛНЕННЫЙ СЕРВЕР ====================

    def _queue_delay(self) -> float:
        """Адаптивная пауза: первые попытки быстрые, дальше реже, с джиттером."""
        base = min(
            QUEUE_MAX_DELAY,
            QUEUE_BASE_DELAY + self.queue_attempts * 3.0,
        )
        # Джиттер только вверх: пауза не должна проваливаться ниже базовой,
        # иначе бот долбит сервер чаще, чем задумано.
        jitter = 1.0 + random.uniform(0.0, QUEUE_JITTER)
        return round(max(2.0, base * jitter), 1)

    def enter_queue(
        self,
        reason: str = "Сервер переполнен",
        increment: bool = True,
        min_delay: float = 0.0,
    ) -> None:
        """Встаёт в очередь и переподключается сам — как лаунчер."""
        if self.stopping or self.is_banned:
            return

        self.queue_mode = True
        self.queue_reason = reason
        if not self.queue_started_at:
            self.queue_started_at = time.time()

        # Таймер очереди уже тикает: кик и дисконнект приходят парой на один
        # и тот же обрыв, второй таймер и вторую попытку не плодим.
        if self._queue_timer_active:
            return

        if increment:
            self.queue_attempts += 1

        delay = max(self._queue_delay(), float(min_delay))
        self.status = f"Очередь #{self.queue_attempts} (вход через {int(delay)}с)"
        self.add_log(
            f"[ОЧЕРЕДЬ] {reason}. Попытка входа #{self.queue_attempts} "
            f"через {delay:.0f}с."
        )
        self.schedule_reconnect(delay=delay)

    def leave_queue(self) -> None:
        """Сброс очереди при успешном входе."""
        if self.queue_mode:
            waited = int(time.time() - self.queue_started_at) if self.queue_started_at else 0
            self.add_log(
                f"[ОЧЕРЕДЬ] Вход разрешён с попытки #{self.queue_attempts} "
                f"(ждал {waited}с)."
            )
        self.queue_mode = False
        self.queue_attempts = 0
        self.queue_started_at = 0.0
        self.queue_reason = ""
        self.connect_failures = 0

    def is_queue_status(self) -> bool:
        return self.queue_mode or self.status.startswith("Очередь")

    # ==================== СПАВН В РЕАЛЬНОМ МИРЕ ====================

    def mark_spawned(self, position=None):
        """Бот реально в мире: статус, лог, метка времени."""
        from logic.spawn import is_spawned_position, nearest_spawn_name, remember_station

        pos = position or (self.api.getbotposition() if self.api else None)
        if not is_spawned_position(pos):
            return False
        remember_station(pos)

        self.spawned_at = time.time()
        self.spawn_point = tuple(float(v) for v in pos)
        self.status = "В игре (заспавнен)"
        try:
            name, dist = nearest_spawn_name(pos)
            label = name or f"вокзал ({pos[0]:.0f}, {pos[1]:.0f})"
            self.add_log(
                f"[SPAWN] на карте: {label} ({dist:.0f}м) "
                f"X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}"
            )
        except Exception:
            pass
        return True

    def begin_spawn_watchdog(self):
        """Доводит спавн до конца: бот должен уйти из зоны авторизации."""
        from logic.spawn import SpawnWatchdog, is_spawned_position

        if self.stopping or self.is_banned:
            return
        if self.spawn_watchdog is not None and self.spawn_watchdog.running:
            return
        # Уже на вокзале — второй хендшейк только дёрнет нас назад.
        # Но статус поставить надо: без него бот навсегда остаётся
        # «В игре (авторизован)», и всё, что смотрит на статус, его не видит.
        pos = self.api.getbotposition() if self.api else None
        if is_spawned_position(pos):
            self.mark_spawned(pos)
            return

        self.spawn_watchdog = SpawnWatchdog(self, log=self.add_log)
        self.spawn_watchdog.start()

    def stop_spawn_watchdog(self):
        if self.spawn_watchdog is not None:
            self.spawn_watchdog.stop()

    def request_spawn(self, index=None):
        """Ручной выбор вокзала: индекс или дефолтный для этого бота."""
        from logic.spawn import force_spawn, spawn_index_for

        idx = spawn_index_for(self.bot_id) if index is None else int(index)
        report = force_spawn(self, index=idx, log=self.add_log)
        self.spawn_report = report
        if report["status"] in ("spawned", "already_spawned"):
            self.mark_spawned(report.get("position"))
        return report

    def setup_session(self):
        self.stopping = False
        self.network = Network(self.host, self.port)
        self.protocol = RakNetProtocol(self.network, security)
        # В ECO-режиме снижаем интервал OnFoot синхронизации для стоячего бота
        # Вместо частых 5 пакетов/сек шлем раз в 0.7-1.2 секунды. Для сервера это валидный OnFoot.
        sync_rate = 0.8 if ECO_80_MODE else 0.2
        self.sync_scheduler = PlayerSyncScheduler(
            rate_player=sync_rate,
            rate_player_min=sync_rate,
            rate_player_max=sync_rate * 1.5,
        )

        reg_config = None
        if self.password:
            reg_config = RegistrationConfig(
                enabled=True,
                auto_login=True,
                password=self.password,
                gender=0,
                skin=78,
                step_delay=float(os.environ.get("BR_AUTH_STEP_DELAY", "0.6")),
                ack_timeout=float(os.environ.get("BR_AUTH_TIMEOUT", "15.0")),
            )

        json_ui = JsonUIConfig(
            console_output=False,
            auto_close_cinematic=True,
        )

        server_names_map = {}
        if self.manager and hasattr(self.manager, "server_names_cache"):
            server_names_map = self.manager.server_names_cache

        self.session = SampSession(
            self.protocol,
            self.host,
            self.port,
            self.nickname,
            sync_scheduler=self.sync_scheduler,
            fps=BOT_NETWORK_FPS,
            pings_ips_continuous=False,
            server_password=None,
            http_pre_ping=True,
            http_pre_ping_port=80,
            http_pre_ping_timeout=5.0,
            http_pre_ping_host_suffix="blackrussia.online",
            http_server_names=server_names_map,
            registration=reg_config,
            json_ui=json_ui,
        )

        self.api = BotAPI(self.session)
        self.network.set_proxy_manager(self.session.proxy)

        # Регистрируем объекты сессии/API в индексе менеджера для O(1) поиска
        if self.manager and hasattr(self.manager, "_obj_index"):
            self.manager._obj_index[id(self.session)] = self.bot_id
            self.manager._obj_index[id(self.api)] = self.bot_id

        if self.proxy:
            try:
                self.session.proxy.connect(self.proxy, timeout=5.0)
                self.add_log(f"Настроен SOCKS5 прокси: {self.proxy}")
            except Exception as e:
                self.add_log(f"Ошибка настройки прокси {self.proxy}: {e}")

    def check_proxy_before_start(self) -> tuple[bool, str]:
        """Сначала запрос, потом заход.

        Если у бота задан прокси, он должен пройти сквозную проверку: TCP до
        внешнего хоста и UDP через STUN. Без UDP бот зайдёт и зависнет в зоне
        авторизации, поэтому такой прокси тоже считается непригодным.

        Прокси не задан — проверять нечего, идём со своего IP.
        """
        if not self.proxy:
            return True, ""
        pool = getattr(self.manager, "proxy_pool", None) if self.manager else None
        if pool is None:
            return True, ""
        try:
            ok, reason = pool.verify_before_connect(self.proxy)
        except Exception as exc:
            return False, f"проверка прокси упала: {exc}"
        if ok:
            self.add_log(f"[ПРОКСИ] проверка пройдена: {reason}")
        else:
            self.add_log(f"[ПРОКСИ] проверка НЕ пройдена: {reason}")
        return ok, reason

    def start(self):
        if self.is_banned:
            self.add_log("Попытка запуска забаненного аккаунта отклонена.")
            return False

        # Прокси проверяем ДО подключения: с мёртвым прокси бот только
        # потратит попытки входа и попадёт в лимит частых подключений.
        ok, reason = self.check_proxy_before_start()
        if not ok:
            self.status = f"Прокси не работает: {reason}"
            self.add_log(f"[-] Запуск отменён, прокси не работает: {reason}")
            return False

        self.cancel_reconnect()
        self.stopping = False
        self.status = "Подключение..."
        self.setup_session()

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

        # Один общий поток статистики на всех ботов вместо потока на каждого.
        if self.manager and hasattr(self.manager, "_ensure_stats_scheduler"):
            self.manager._ensure_stats_scheduler()

        return True

    def _stats_tick(self, now: float) -> None:
        """Один тик проверки статистики (вызывается общим шедулером менеджера)."""
        if self.stopping or self.is_banned:
            return
        if not (self.session and self.session.connected):
            return
        if "В игре" not in self.status:
            return
        if now - self.last_stats_check >= 1800:
            self.stats_pending = True
            self.send_chat("/mm")
            self.last_stats_check = now

    def _run(self):
        try:
            # Гейт: не больше N одновременных хэндшейков на сервер.
            gate = getattr(self.manager, "connect_gate", None)
            gate_held = False
            if gate is not None:
                gate_held = gate.wait_turn(self.host)
            try:
                connected = self.api.connect()
            finally:
                if gate_held and gate is not None:
                    gate.release(self.host)

            if connected:
                self.connect_failures = 0
                return

            if self.stopping or self.is_banned:
                return

            # Реконнект уже запланирован обработчиком кика/дисконнекта —
            # второй таймер не нужен, иначе попытки считаются дважды.
            if self.reconnect_thread and self.reconnect_thread.is_alive():
                return

            self.connect_failures += 1

            # Сервер молчит: чаще всего это переполнение. Встаём в очередь.
            if self.connect_failures >= 2 or self.queue_mode:
                self.enter_queue("Сервер не отвечает (переполнен)", increment=True)
            else:
                retry_delay = round(random.uniform(RECONNECT_MIN, RECONNECT_MAX), 1)
                self.add_log(
                    f"Сервер не ответил на подключение. Повтор через {retry_delay}с..."
                )
                self.schedule_reconnect(delay=retry_delay)
        except Exception as e:
            if not self.stopping and not self.is_banned:
                self.status = f"Ошибка: {e}"
                self.add_log(f"Критическая ошибка сессии: {e}")
                self.schedule_reconnect(delay=10.0)

    def schedule_reconnect(self, delay: float = 10.0, force: bool = False):
        if self.stopping or self.is_banned:
            return

        # Кик и дисконнект на один обрыв приходят парой, плюс _run может
        # успеть следом. Флаг ставим синхронно под локом — поток мог ещё
        # не стартовать, и is_alive() тут не спасёт.
        #
        # force — перебить уже поставленный таймер. Нужен, когда сервер
        # сказал «аккаунт в сети»: обычная пауза 8-20с короче, чем сервер
        # держит призрачную сессию, и вход в неё превращается в двойную
        # сессию. За это античит кикает с кодом 25.
        with self._sched_lock:
            if self._reconnect_pending and not force:
                return
            self._reconnect_pending = True
            self._sched_generation += 1
            my_gen = self._sched_generation

        # Гасим предыдущий таймер, не трогая наш только что поставленный флаг
        self.reconnect_cancel_event.set()
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            try:
                self.reconnect_thread.join(timeout=0.2)
            except Exception:
                pass

        self.reconnect_cancel_event.clear()
        target_time = time.time() + delay
        self._queue_timer_active = bool(self.queue_mode)
        if self.queue_mode:
            self.add_log(
                f"Очередь: следующая попытка входа через {int(delay)} сек..."
            )
        else:
            self.add_log(f"Связь разорвана/ожидание. Авто-вход через {int(delay)} сек...")

        def _reconnect_waiter():
            try:
                while time.time() < target_time:
                    if self.reconnect_cancel_event.is_set() or self.stopping or self.is_banned:
                        return
                    rem = max(0, int(target_time - time.time()))
                    m, s = divmod(rem, 60)
                    if self.queue_mode:
                        self.status = f"Очередь #{self.queue_attempts} ({m:02d}:{s:02d})"
                    else:
                        self.status = f"Ожидание ({m:02d}:{s:02d})"
                    time.sleep(1.0)

                if self.reconnect_cancel_event.is_set() or self.stopping or self.is_banned:
                    return

                if self.queue_mode:
                    self.add_log(
                        f"[ОЧЕРЕДЬ] Попытка входа #{self.queue_attempts} — подключаемся..."
                    )
                else:
                    self.add_log(f"Ожидание завершено ({int(delay)} сек). Автоматический перезапуск сессии...")
                self.start()
            finally:
                # Таймер отработал (или отменён). Флаг снимает только тот
                # таймер, чья генерация ещё актуальна — иначе старый поток
                # затрёт флаг новому.
                self._queue_timer_active = False
                with self._sched_lock:
                    if self._sched_generation == my_gen:
                        self._reconnect_pending = False

        self.reconnect_thread = threading.Thread(target=_reconnect_waiter, daemon=True)
        self.reconnect_thread.start()

    def cancel_reconnect(self):
        self.reconnect_cancel_event.set()
        with self._sched_lock:
            self._reconnect_pending = False

    def send_chat(self, msg: str):
        if self.api:
            self.api.sendchat(msg)
            self.add_log(f">> {msg}")

    def set_task(self, task_type: str):
        if self.active_task:
            self.active_task.stop()
            self.active_task = None

        self.default_task_name = task_type
        if self.manager:
            self.manager.save_accounts()

        if task_type == "lvl":
            self.active_task = LevelGrinderTask(self)
            self.active_task.start()
            return "Качка LVL запущена"
        elif task_type == "quest":
            self.active_task = QuestMasterTask(self)
            self.active_task.start()
            return "Квесты Дяди Славы запущены"
        elif task_type in ("mine", "miner", "mining", "шахта"):
            self.active_task = MinerTask(self)
            self.active_task.start()
            return "Бот шахтера запущен"
        elif task_type in ("factory", "zavod", "завод"):
            self.active_task = FactoryTask(self)
            self.active_task.start()
            return "Бот завода запущен"
        elif task_type in ("bus", "автобус", "driver"):
            self.active_task = BusDriverTask(self)
            self.active_task.start()
            return "Бот водителя автобуса запущен"
        elif task_type in ("stop", "none", "off"):
            return "Авто-задача остановлена"
        else:
            return "Неизвестный тип задачи. Доступно: 'lvl', 'quest', 'mine', 'factory', 'bus', 'stop'"

    def stop(self, banned: bool = False):
        self.stopping = True
        self.cancel_reconnect()
        self.stop_spawn_watchdog()
        self.queue_mode = False
        self.queue_reason = ""
        self._queue_timer_active = False
        if self.active_task:
            try:
                self.active_task.stop()
            except Exception:
                pass
            self.active_task = None
        self.status = "ЗАБАНЕН" if (banned or self.is_banned) else "Отключен"
        if self.api:
            try:
                self.api.disconnect()
            except Exception:
                pass


class BotManager:
    def __init__(self):
        self._ensure_br_alias()
        self.bots: dict[int, BotInstance] = {}
        self._next_id = 1
        self.active_bot_id: int | None = None
        self._lock = threading.Lock()
        self.stream_active_logs = True

        # Гейт подключений: 30 ботов не входят лавиной (иначе кик за частые входы)
        self.connect_gate = ConnectGate()
        # Общий поток статистики (вместо потока на каждого бота)
        self._stats_thread = None
        # Индекс ник -> бот, чтобы не сканировать всех на каждой строке лога
        self._nick_index: dict[str, BotInstance] = {}
        # Индекс id(session/api) -> bot_id: O(1) на событиях пакетов
        self._obj_index: dict[int, int] = {}
        # Дебаунс записи accounts.json
        self._save_lock = threading.Lock()
        self._last_save = 0.0
        self._save_pending = False

        # Кэш соответствия IP серверов и их названий (для правильного HTTP pre-ping заголовка Host)
        self.server_names_cache = {}
        try:
            all_s = br_servers.get_servers()
            self.server_names_cache = {s["ip"]: s.get("firstname", "") for s in all_s}
        except Exception:
            self.server_names_cache = {}

        logger.set_log_interceptor(self._handle_global_log)
        self._setup_event_hooks()

        # Пул прокси: подписки, ручные прокси и их проверка по таймеру.
        # Проверка идёт в фоне раз в 5 минут, чтобы к моменту запуска бота
        # уже было известно, какой прокси живой.
        try:
            from logic.proxy_pool import ProxyPool

            self.proxy_pool = ProxyPool(
                PROXY_POOL_FILE,
                log=lambda msg: print(msg, flush=True),
                interval=PROXY_REFRESH_INTERVAL,
            )
            self.proxy_pool.start()
        except Exception as exc:
            self.proxy_pool = None
            print(f"[-] Пул прокси не поднялся: {exc}")

        # Загружаем сохраненные аккаунты из accounts.json
        self.load_accounts()

    def _ensure_br_alias(self):
        """Автоматически регистрирует команду 'br' в системе при первом запуске."""
        try:
            repo_dir = os.path.dirname(os.path.abspath(__file__))
            candidates = [
                "/data/data/com.termux/files/usr/bin/br",
                os.path.expanduser("~/.local/bin/br"),
                "/usr/local/bin/br"
            ]
            for target in candidates:
                target_dir = os.path.dirname(target)
                if os.path.isdir(target_dir) and os.access(target_dir, os.W_OK):
                    script_content = (
                        "#!/bin/bash\n"
                        f'cd "{repo_dir}" && exec python3 bot.py "$@"\n'
                    )
                    need_write = True
                    if os.path.exists(target):
                        try:
                            with open(target, "r", encoding="utf-8") as f:
                                if f.read() == script_content:
                                    need_write = False
                        except Exception:
                            pass
                    if need_write:
                        with open(target, "w", encoding="utf-8") as f:
                            f.write(script_content)
                        os.chmod(target, 0o755)
                    break
        except Exception:
            pass

    def _ensure_stats_scheduler(self) -> None:
        """Один поток статистики на весь ботнет. Экономит N-1 потоков."""
        if self._stats_thread and self._stats_thread.is_alive():
            return
        self._stats_thread = threading.Thread(
            target=self._stats_scheduler_loop,
            daemon=True,
            name="stats-scheduler",
        )
        self._stats_thread.start()

    def _stats_scheduler_loop(self) -> None:
        last_gc = time.time()
        while True:
            time.sleep(5.0)
            now = time.time()
            with self._lock:
                bots = list(self.bots.values())
            for b in bots:
                try:
                    b._stats_tick(now)
                except Exception:
                    pass
            # Дописываем отложенные изменения accounts.json
            try:
                self.flush_pending_save()
            except Exception:
                pass

            # Периодический сборщик мусора (GC) для стабильной работы 80+ ботов в Termux
            if now - last_gc >= 180.0:
                try:
                    import gc
                    gc.collect()
                    last_gc = now
                except Exception:
                    pass

    def _handle_global_log(self, raw_message: str) -> bool:
        assigned = False
        nick_match = _LOG_NICK_RE.search(raw_message)
        nick = nick_match.group(1) if nick_match else None

        with self._lock:
            bot = self._nick_index.get(nick) if nick else None
            if bot is not None:
                bot.logs.append(raw_message)
                assigned = True
            else:
                for b in self.bots.values():
                    if b.nickname and b.nickname in raw_message:
                        b.logs.append(raw_message)
                        assigned = True
                        break

            if not assigned and self.active_bot_id is not None:
                cur = self.bots.get(self.active_bot_id)
                if cur:
                    cur.logs.append(raw_message)

            if not self.stream_active_logs:
                return False

            if self.active_bot_id is not None:
                cur = self.bots.get(self.active_bot_id)
                if cur and cur.nickname and cur.nickname in raw_message:
                    return True
                # Если лог не привязан к активному боту, не выводим в общую консоль!
                return False
            # Если активный бот не выбран, подавляем шум подключения/киков
            return False

    def _find_bot_by_session_or_api(self, bot_obj):
        """O(1) поиск бота по объекту сессии/API.

        Вызывается на каждом событии пакета — при 30 ботах линейный
        перебор по всем ботам тут был бы главным пожирателем CPU.
        """
        bot_id = self._obj_index.get(id(bot_obj))
        if bot_id is not None:
            bot = self.bots.get(bot_id)
            if bot is not None:
                return bot

        with self._lock:
            for b in self.bots.values():
                if b.api == bot_obj or b.session == bot_obj:
                    self._obj_index[id(bot_obj)] = b.bot_id
                    return b
        return None

    def mark_bot_banned(self, bot: BotInstance, reason: str = ""):
        bot.is_banned = True
        bot.cancel_reconnect()
        bot.status = "ЗАБАНЕН"
        bot.add_log(f"[!] АККАУНТ ЗАБАНЕН СЕРВЕРОМ! Причина: {reason}")
        print(f"\n[!] ВНИМАНИЕ: Бот #{bot.bot_id} [{bot.nickname}] получил БАН! ({reason})")
        bot.stop(banned=True)
        self.save_accounts()

        # 1. Отправляем в Telegram и ИИ-анализ если подключен Telegram контроллер
        if hasattr(self, "tg_controller") and self.tg_controller:
            alert_text = (
                f"🚨 <b>АККАУНТ ЗАБАНЕН!</b>\n"
                f"• Бот: #{bot.bot_id} <b>{bot.nickname}</b>\n"
                f"• Сервер: <b>{bot.server_name}</b>\n"
                f"• Причина: <code>{reason}</code>\n"
            )
            # Отправляем в ИИ для анализа причины и рекомендаций
            if hasattr(self.tg_controller, "ai") and self.tg_controller.ai.config.get("api_key"):
                def _ai_ban_inspect():
                    ai_res = self.tg_controller.ai.execute_natural_command(
                        f"Бот {bot.nickname} на сервере {bot.server_name} получил бан по причине: '{reason}'. Проанализируй причину."
                    )
                    self.tg_controller.broadcast(f"{alert_text}\n🤖 <b>Анализ ИИ:</b>\n{ai_res}")
                threading.Thread(target=_ai_ban_inspect, daemon=True).start()
            else:
                self.tg_controller.broadcast(alert_text)

        # 2. Если бан выдан именно за бота / читы / автокликер — создаем новый аккаунт на том же сервере
        r_lower = reason.lower()
        if any(w in r_lower for w in ["бот", "bot", "читер", "чит", "постороннее по", "кликер", "auto-clicker", "connection_banned"]):
            def _create_replacement():
                time.sleep(random.uniform(5.0, 10.0))
                print(f"[*] Создаем новый аккаунт взамен забаненного бота на сервере {bot.server_name}...")
                new_bot = self.add_bot(
                    target_server=bot.server_name,
                    nickname=None,  # None -> сгенерировать реалистичный ник
                    password=bot.password,
                    auto_regen_nick=True,
                    proxy=bot.proxy,
                    default_task=bot.default_task_name,
                )
                if new_bot:
                    notice = (
                        f"♻️ <b>Автозамена забаненного бота!</b>\n"
                        f"На сервере <b>{bot.server_name}</b> создан новый бот: #{new_bot.bot_id} <b>{new_bot.nickname}</b>.\n"
                        f"Задача: <code>{new_bot.default_task_name}</code>. Запускаем..."
                    )
                    if hasattr(self, "tg_controller") and self.tg_controller:
                        self.tg_controller.broadcast(notice)
                    new_bot.start()
            threading.Thread(target=_create_replacement, daemon=True).start()

    def start_monitor_loop(self):
        """Запуск непрерывного 24/7 фонового мониторинга ботнета"""
        if getattr(self, "_monitor_running", False):
            return
        self._monitor_running = True
        def _loop():
            print("[*] Непрерывный мониторинг 24/7 запущен.")
            while self._monitor_running:
                time.sleep(60.0)
                try:
                    with self._lock:
                        for bid, b in self.bots.items():
                            # Автоматический перезапуск упавших активных ботов
                            if not b.is_banned and not b.stopping:
                                if b.status in ("Остановлен", "Отключен") and b.default_task_name:
                                    b.add_log("[МОНИТОР] Авто-подъем упавшего бота 24/7...")
                                    b.start()
                except Exception as e:
                    print(f"[-] Ошибка монитора: {e}")
        threading.Thread(target=_loop, daemon=True).start()

    def _setup_event_hooks(self):
        @emitter.on("onconnect")
        def _on_connected(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                # Новая сессия — старое меню вокзалов недействительно
                from logic.spawn import reset_menu

                reset_menu()
                # Вход разрешён — очередь сброшена
                b.leave_queue()
                b.status = "В игре (авторизован)"
                b.connected_time = time.time()
                b.add_log("Подключение установлено, мир загружен")
                if b.default_task_name and not b.active_task:
                    b.set_task(b.default_task_name)

        @emitter.on("onSpawn")
        def _on_spawn(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.leave_queue()
                # Статус «заспавнен» ставим только когда бот реально в мире,
                # а не когда мы сами отправили RPC_SPAWN.
                b.begin_spawn_watchdog()

        @emitter.on("onSpawnSelect")
        def _on_spawn_select(bot_obj, data, *args, **kwargs):
            """Сервер прислал меню вокзалов — сразу выбираем первый."""
            b = self._find_bot_by_session_or_api(bot_obj)
            if not b:
                return

            from logic.spawn import (
                available_spawns,
                in_auth_zone,
                is_spawn_menu,
                menu_choice,
                remember_spawn_menu,
                select_spawn,
            )

            payload = data if isinstance(data, dict) else {}

            if is_spawn_menu(payload):
                items = remember_spawn_menu(payload)
                choice = menu_choice(items)
                b.add_log(f"[SPAWN] меню вокзалов {items}, выбираем {choice}")
                # Отвечаем прямо здесь: меню живёт несколько секунд.
                if select_spawn(b, choice):
                    b.add_log(f"[SPAWN] отправлен выбор вокзала {choice}")
                    # Сторож нужен только если спавн не состоится: он сам
                    # выждет меню-паузу и не полезет с хендшейком раньше.
                    if b.spawn_watchdog is None or not b.spawn_watchdog.running:
                        b.begin_spawn_watchdog()
                return

            # {'s': N} — либо приглашение, либо подтверждение выбора.
            # Приглашение {"s": 1} без списка сервер не обслуживает,
            # вокзал выбирается RPC-хендшейком.
            if not payload.get("s"):
                return

            pos = b.api.getbotposition() if b.api else None
            if available_spawns() and in_auth_zone(pos):
                if b.spawn_watchdog is None or not b.spawn_watchdog.running:
                    b.begin_spawn_watchdog()

        @emitter.on("onShowChat")
        def _on_chat(bot_obj, message, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ЧАТ] {message}")
                if b.active_task and hasattr(b.active_task, "on_chat_message"):
                    b.active_task.on_chat_message(str(message))
                msg_lower = str(message).lower()
                clean_msg = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(message))
                clean_msg_lower = clean_msg.lower()

                # 0. Сервер сообщил о переполнении -> встаём в очередь как лаунчер
                if any(k in clean_msg_lower for k in FULL_SERVER_MARKERS):
                    b.enter_queue(f"Сервер переполнен: {clean_msg[:60]}")
                    return

                # 1. Проверяем бан аккаунта / блокировку администратором
                # Примеры: "Администратор X забанил Y", "Вы были заблокированы", "аккаунт забанен"
                admin_ban_patterns = [
                    r'администратор\s+.*\s+(?:забанил|заблокировал|выдал\s+бан)\s+.*' + re.escape(b.nickname.lower()),
                    r'(?:вы\s+были\s+)?заблокированы?\s+администратор',
                    r'аккаунт\s+.*заблокирован',
                ]
                is_admin_ban = any(re.search(pat, clean_msg_lower) for pat in admin_ban_patterns)
                if not is_admin_ban and b.nickname.lower() in clean_msg_lower:
                    ban_words = ["заблокирован", "забанен", "получил бан", "выдан бан", "banned"]
                    if any(k in clean_msg_lower for k in ban_words) and "чат" not in clean_msg_lower and "мут" not in clean_msg_lower and "20 мин" not in clean_msg_lower and "частые" not in clean_msg_lower:
                        is_admin_ban = True

                # 2. Бан / ограничение на 15-20 минут за частые входы/подключения
                # Сервер может написать: "вы были заблокированы за частые подключения", "подождите 20 минут", "частые входы", "повторите попытку через 20 минут"
                freq_ban_keywords = [
                    "20 минут", "20 мин", "15 минут", "15 мин",
                    "частые входы", "частых входов", "частые подключения", "частых подключений",
                    "слишком много подключений", "подозрение на спам", "повторите попытку через"
                ]
                is_freq_ban = any(k in clean_msg_lower for k in freq_ban_keywords)

                if is_freq_ban:
                    b.add_log(f"[РЕКОННЕКТ] Бан за частые входы/лимит времени (20 мин): '{clean_msg}'. Вход через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0
                    return

                if is_admin_ban:
                    self.mark_bot_banned(b, reason=f"Чат: {clean_msg}")
                    return

                # 3. Много заходов с вашего IP -> реконнект через 5 минут (300 сек)
                if "много заходов" in clean_msg_lower or ("ip" in clean_msg_lower and ("слишком много" in clean_msg_lower or "подозрител" in clean_msg_lower or "повторите попытку позже" in clean_msg_lower)):
                    b.add_log("[РЕКОННЕКТ] Сервер сообщил: много заходов с IP. Ожидание 5 минут...")
                    b.scheduled_reconnect_delay = 5 * 60.0

                # 4. Рестарт сервера через 1 минуту -> выход через 30 сек, реконнект через 5 минут
                if "рестарт" in clean_msg_lower and ("через 1 мин" in clean_msg_lower or "1 минуту" in clean_msg_lower or "60 сек" in clean_msg_lower):
                    b.add_log("[РЕСТАРТ] Обнаружено предупреждение о рестарте через 1 минуту. Выход через 30 секунд...")
                    def _safe_restart_exit():
                        time.sleep(30.0)
                        if b and not b.is_banned:
                            b.add_log("[РЕСТАРТ] Выход с сервера перед рестартом. Вход запланирован через 5 минут.")
                            b.scheduled_reconnect_delay = 5 * 60.0
                            b.stop()
                    threading.Thread(target=_safe_restart_exit, daemon=True).start()

        @emitter.on("onShowDialog")
        def _on_dialog(bot_obj, d_id, style, title, info, b1, b2, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ДИАЛОГ #{d_id}] {title} | Кнопки: '{b1}' / '{b2}'")
                if b.active_task and hasattr(b.active_task, "on_dialog"):
                    b.active_task.on_dialog(d_id, style, title, info, b1, b2)

                dialog_text = f"{title} {info}".lower()
                clean_info = re.sub(r'\{[0-9a-fA-F]{6}\}', '', info)

                # 0. Диалог о переполнении сервера -> очередь как в лаунчере
                if any(k in dialog_text for k in FULL_SERVER_MARKERS):
                    b.add_log(f"[ОЧЕРЕДЬ] Сервер переполнен (диалог #{d_id}): {title}")
                    # Закрываем диалог, чтобы не висел
                    def _close_full_dialog():
                        time.sleep(0.4)
                        from samp.packets import PacketBuilder
                        if b.session and b.session.connected:
                            try:
                                b.session._sender.send(
                                    PacketBuilder.rpc_build("dialog_response", int(d_id), 0, 0, ""),
                                    rpc_name="dialog_response",
                                )
                            except Exception:
                                pass
                    threading.Thread(target=_close_full_dialog, daemon=True).start()
                    b.enter_queue("Сервер переполнен (диалог)")
                    return

                # 1. Автоматический опрос /mm -> Статистика
                # Меню /mm: выбор "Статистика персонажа" (обычно 0 пункт)
                if ("главное меню" in dialog_text or "меню персонажа" in dialog_text or "mm" in dialog_text) and b.stats_pending:
                    # Находим пункт "Статистика" в списке
                    lines = [ln.strip() for ln in clean_info.split("\n") if ln.strip()]
                    stat_idx = 0
                    for idx, ln in enumerate(lines):
                        if "статистик" in ln.lower():
                            stat_idx = idx
                            break
                    def _pick_stat():
                        time.sleep(0.4)
                        from samp.packets import PacketBuilder
                        if b.session and b.session.connected:
                            b.session._sender.send(
                                PacketBuilder.rpc_build("dialog_response", int(d_id), 1, stat_idx, ""),
                                rpc_name="dialog_response",
                            )
                    threading.Thread(target=_pick_stat, daemon=True).start()

                # Окно со статистикой персонажа (уровень, exp)
                if "статистика" in dialog_text or "уровень" in dialog_text or "exp" in dialog_text:
                    # Ищем уровень и exp: например "Уровень: 2", "Игровой уровень: 2", "Очки опыта: 2/8", "EXP: 2/8", "Опыт: 2 из 8"
                    # Также форматы: "2 lvl", "2/8 exp"
                    lvl_match = re.search(r'(?:уровень|level|lvl)[:\s]+(\d+)', clean_info, re.IGNORECASE)
                    exp_match = re.search(r'(?:exp|опыт|очки опыта|очков)[:\s]+(\d+)\s*[\/из]\s*(\d+)', clean_info, re.IGNORECASE)
                    
                    found_lvl = lvl_match.group(1) if lvl_match else None
                    found_exp = f"{exp_match.group(1)}/{exp_match.group(2)}" if exp_match else None

                    if not found_exp:
                        # Иногда формат просто 2/8
                        exp_fallback = re.search(r'(\d+)\s*\/\s*(\d+)', clean_info)
                        if exp_fallback:
                            found_exp = f"{exp_fallback.group(1)}/{exp_fallback.group(2)}"

                    # Ищем деньги / баланс: например "Баланс: 15,000 руб", "Деньги: 1000", "Наличные: 500 руб"
                    # В диалогах Black Russia часто пишется: "Наличные деньги: 15000 руб.", "Банковский счет: 20000 руб.", "Баланс: 500"
                    money_matches = re.findall(r'(?:баланс|деньги|наличные|рублей|на руках|основной счет|банк(?:\s+счет)?)[^\d\n\r]{0,15}?([\d\s,]+)', clean_info, re.IGNORECASE)
                    if money_matches:
                        for mm in money_matches:
                            raw_m = re.sub(r'[^\d]', '', mm)
                            if raw_m:
                                val = int(raw_m)
                                if val > 0:
                                    b.money = val
                                    break
                    else:
                        money_match = re.search(r'([\d\s,]{2,})\s*(?:руб|rub|\$)', clean_info, re.IGNORECASE)
                        if money_match:
                            raw_m = re.sub(r'[^\d]', '', money_match.group(1))
                            if raw_m and int(raw_m) > 0:
                                b.money = int(raw_m)

                    if found_lvl:
                        if found_exp:
                            b.level_str = f"{found_lvl} ({found_exp})"
                        else:
                            b.level_str = f"{found_lvl} lvl"
                        b.last_stats_check = time.time()
                        b.stats_pending = False
                        b.add_log(f"[СТАТИСТИКА] Обновлен уровень: {b.level_str}, баланс: {b.get_balance_str()}")
                        self.save_accounts()
                        b.check_target_level_achieved()

                    # Закрываем диалог статистики, чтобы не висел
                    def _close_stats():
                        time.sleep(0.3)
                        from samp.packets import PacketBuilder
                        if b.session and b.session.connected:
                            b.session._sender.send(
                                PacketBuilder.rpc_build("dialog_response", int(d_id), 0, 0, ""),
                                rpc_name="dialog_response",
                            )
                    threading.Thread(target=_close_stats, daemon=True).start()

                # Проверяем диалоговые окна на бан / лимиты входа / рестарт
                ban_triggers = [
                    "блокировк",
                    "заблокирован",
                    "забанен",
                    "бан навсегда",
                    "аккаунт заблокирован",
                    "banned",
                    "причина блокировки",
                    "до разблокировки",
                    "вы получили бан",
                    "ваш аккаунт был заблокирован",
                    "подозрение в использовании чит",
                    "постороннее по",
                    "connection_banned",
                    "чит-программ",
                    "чит программа",
                    "читы/ппв",
                    "использование бот",
                    "auto-clicker",
                    "автокликер",
                ]
                # Короткие слова проверяем по границам слова, иначе "работы"
                # (Информация о работах) ловится на подстроку "бот" и ферма
                # мгновенно уходит в ложный бан.
                ban_patterns = [
                    re.compile(r"\bбот(?:ы|а|ов|ом|е)?\b", re.IGNORECASE),
                    re.compile(r"\bчит(?:ы|ов|ами|ер)?\b", re.IGNORECASE),
                ]
                # Исключаем временный бан за частые входы из перманентного бана
                freq_dialog_keywords = [
                    "20 минут", "20 мин", "15 минут", "15 мин",
                    "частые входы", "частых входов", "частые подключения", "частых подключений",
                    "слишком много подключений", "повторите попытку через"
                ]
                is_freq_ban = any(k in dialog_text for k in freq_dialog_keywords)
                if is_freq_ban:
                    b.add_log(f"[РЕКОННЕКТ] Бан за частые входы в диалоге #{d_id}. Вход через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0
                elif any(trigger in dialog_text for trigger in ban_triggers) or any(
                    pattern.search(dialog_text) for pattern in ban_patterns
                ):
                    self.mark_bot_banned(b, reason=f"Диалог #{d_id}: {title} | {clean_info[:80]}")

                # Много заходов с одного IP в диалоге
                if "много заходов" in dialog_text or ("ip" in dialog_text and ("слишком много" in dialog_text or "подозрител" in dialog_text or "повторите попытку позже" in dialog_text)):
                    b.add_log(f"[РЕКОННЕКТ] Диалог #{d_id}: много заходов с IP. Ожидание 5 минут...")
                    b.scheduled_reconnect_delay = 5 * 60.0

        @emitter.on("OnShowTextDraw")
        def _on_textdraw(bot_obj, textdraw_id, info, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_textdraw"):
                b.active_task.on_textdraw(textdraw_id, info)

        @emitter.on("OnTextDrawSetString")
        def _on_td_set_string(bot_obj, textdraw_id, text, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_textdraw_set_string"):
                b.active_task.on_textdraw_set_string(textdraw_id, text)

        @emitter.on("disconnect")
        def _on_disconnect(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log("Соединение с сервером разорвано")
                if b.is_banned:
                    b.status = "ЗАБАНЕН"
                    return
                if b.stopping:
                    b.status = "Отключен"
                    return

                # Уже в очереди на переполненный сервер — таймер уже идёт
                if b.queue_mode:
                    return

                # Проверяем, был ли задан кастомный таймер ожидания (рестарт, лимит IP, бан за частые входы)
                if b.scheduled_reconnect_delay:
                    reconnect_delay = b.scheduled_reconnect_delay
                    b.scheduled_reconnect_delay = None
                else:
                    # Разброс паузы, чтобы 30 ботов не входили одновременно
                    reconnect_delay = round(
                        random.uniform(RECONNECT_MIN, RECONNECT_MAX), 1
                    )

                b.schedule_reconnect(delay=reconnect_delay)

        @emitter.on("onKick")
        def _on_kick(bot_obj, reason_code, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[КИК] Отключен сервером. Код причины: {reason_code}")
                if reason_code == 999:
                    self.mark_bot_banned(b, reason="Пакет CONNECTION_BANNED (0x19)")
                    return
                if reason_code == 2 and getattr(b, "auto_regen_nick", False):
                    self.replace_bot_nickname(b)
                    return
                # 2 = аккаунт уже в сети. Сервер ещё держит призрачную сессию —
                # ломиться сразу бессмысленно, он будет кикать по кругу.
                if reason_code == 2:
                    wait = round(random.uniform(45.0, 90.0), 1)
                    b.status = f"Сессия занята (повтор через {int(wait)}с)"
                    b.add_log(
                        f"[РЕКОННЕКТ] Аккаунт уже в сети. Ждём {int(wait)}с, "
                        f"пока сервер отпустит старую сессию."
                    )
                    b.schedule_reconnect(delay=wait, force=True)
                    return
                # 4 = нет свободных слотов -> встаём в очередь и заходим сами
                if reason_code == 4:
                    b.enter_queue("Сервер переполнен (нет свободных слотов)")
                    return
                b.status = f"Кикнут (код {reason_code})"

        @emitter.on("onRegistrationState")
        def _on_reg_state(bot_obj, state, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[АВТОРИЗАЦИЯ] Статус: {state}")
                if state == "logging_in" and getattr(b, "auto_regen_nick", False):
                    self.replace_bot_nickname(b)
                if state == "authenticated":
                    # Только после логина имеет смысл выбирать вокзал
                    b.begin_spawn_watchdog()

        @emitter.on("onCallNotification")
        def _on_call(bot_obj, header, text, button, notif_type, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ВХОДЯЩИЙ ЗВОНОК] {header}: {text}")
                try:
                    b.quest_helpers.on_json(
                        65,
                        {"o": 1, "t": notif_type, "h": header, "s": text, "b": button},
                    )
                except Exception as exc:
                    b.add_log(f"[ЗВОНОК] помощник упал: {exc!r}")
                if b.active_task and hasattr(b.active_task, "on_call_notification"):
                    b.active_task.on_call_notification(header, text, button, notif_type)

        @emitter.on("onReceiveJSON")
        def _on_receive_json(bot_obj, interface_id, json_data, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if not b:
                return
            # Помощники сюжетки видят всё: диалоги НПС, ларёк, звонки, палитру.
            try:
                handled = b.quest_helpers.on_json(interface_id, json_data)
            except Exception as exc:
                b.add_log(f"[ПОМОЩНИК] сбой на iid={interface_id}: {exc!r}")
                handled = None
            if b.active_task and hasattr(b.active_task, "on_receive_json"):
                b.active_task.on_receive_json(interface_id, json_data)
            return handled

        @emitter.on("onQuestTask")
        def _on_quest_task(bot_obj, text, *args, **kwargs):
            # _emit_callback подставляет объект бота первым аргументом.
            # Раньше здесь не было bot_obj, и в text приезжал сам бот —
            # задача получала объект вместо текста квеста.
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and isinstance(b.active_task, QuestMasterTask):
                b.active_task.on_quest_task(text)

        @emitter.on("onNpcDialog")
        def _on_npc_dialog(bot_obj, name, text, model, buttons, *args, **kwargs):
            # Тот же сдвиг: без bot_obj имя НПС оказывалось в bot_obj,
            # текст в name, модель в text, а кнопки терялись в *args.
            b = self._find_bot_by_session_or_api(bot_obj)
            if not b:
                return
            try:
                b.quest_helpers.on_npc_dialog(name, text, buttons, model)
            except Exception as exc:
                b.add_log(f"[НПС] помощник упал: {exc!r}")
            if b.active_task and hasattr(b.active_task, "on_npc_dialog"):
                b.active_task.on_npc_dialog(name, text, model, buttons)

        @emitter.on("OnSetCheckpoint")
        def _on_checkpoint(bot_obj, x, y, z, radius, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_checkpoint"):
                b.active_task.on_checkpoint(x, y, z, radius)

        @emitter.on("OnSetMoney")
        def _on_set_money(bot_obj, amount, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                try:
                    b.money = int(amount)
                    b.add_log(f"[БАЛАНС] Обновлен баланс: {b.get_balance_str()}")
                    self.save_accounts()
                except Exception:
                    pass

        @emitter.on("onRewardList")
        def _on_rewards(bot_obj, rewards, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and isinstance(b.active_task, QuestMasterTask):
                b.active_task.on_rewards(rewards)

    def load_accounts(self):
        if not os.path.exists(ACCOUNTS_FILE):
            return

        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            loaded_count = 0
            for item in data:
                nick = item.get("nickname", "").strip()
                pwd = item.get("password", "").strip()
                srv_str = item.get("server", "").strip()
                task = item.get("task", "lvl")
                is_banned = bool(item.get("is_banned", False))
                proxy = item.get("proxy", "")
                saved_lvl = item.get("lvl", "-")
                saved_money = int(item.get("money", 0))
                saved_target_lvl = item.get("target_lvl")
                if saved_target_lvl is not None:
                    try:
                        saved_target_lvl = int(saved_target_lvl)
                    except Exception:
                        saved_target_lvl = None

                if not nick or not pwd or not srv_str:
                    continue

                try:
                    srv = br_servers.find_server(srv_str)
                except Exception:
                    srv = None

                if not srv:
                    continue

                clean_name = srv.get("firstname") or srv.get("name", "").replace("BLACK RUSSIA | ", "").strip()
                clean_name = clean_name.upper()
                ip = srv["ip"]
                port = int(srv["port"])

                with self._lock:
                    bot_id = self._next_id
                    self._next_id += 1
                    b = BotInstance(
                        bot_id=bot_id,
                        host=ip,
                        port=port,
                        nickname=nick,
                        password=pwd,
                        server_name=clean_name,
                        auto_regen_nick=False,
                        manager=self,
                        is_banned=is_banned,
                        default_task=task,
                        proxy=proxy,
                    )
                    b.level_str = saved_lvl
                    b.money = saved_money
                    b.target_lvl = saved_target_lvl
                    self.bots[bot_id] = b
                    if self.active_bot_id is None:
                        self.active_bot_id = bot_id
                loaded_count += 1

            if loaded_count > 0:
                print(f"[+] Загружено {loaded_count} сохраненных аккаунтов из {os.path.basename(ACCOUNTS_FILE)}")
        except Exception as e:
            print(f"[!] Ошибка загрузки {ACCOUNTS_FILE}: {e}")
        finally:
            self._rebuild_nick_index()

    def _forget_bot(self, bot: BotInstance) -> None:
        """Убирает бота из индексов (при удалении/замене ника)."""
        self._nick_index.pop(bot.nickname, None)
        if bot.session is not None:
            self._obj_index.pop(id(bot.session), None)
        if bot.api is not None:
            self._obj_index.pop(id(bot.api), None)

    def _rebuild_nick_index(self) -> None:
        """Пересобирает индекс ник -> бот. Вызывать после любых изменений состава ботов."""
        with self._lock:
            self._nick_index = {
                b.nickname: b for b in self.bots.values() if b.nickname
            }

    def save_accounts(self, force: bool = False):
        """Пишет accounts.json. Без force — не чаще раза в 10 секунд (бережём флеш)."""
        now = time.time()
        with self._save_lock:
            if not force and (now - self._last_save) < SAVE_DEBOUNCE:
                self._save_pending = True
                return
            self._last_save = now
            self._save_pending = False

        with self._lock:
            data = []
            for b in self.bots.values():
                data.append({
                    "nickname": b.nickname,
                    "password": b.password,
                    "server": b.server_name,
                    "task": b.default_task_name or (b.active_task.name if b.active_task else "lvl"),
                    "is_banned": b.is_banned,
                    "proxy": b.proxy,
                    "lvl": b.level_str,
                    "money": b.money,
                    "target_lvl": b.target_lvl,
                })

        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[!] Ошибка сохранения {ACCOUNTS_FILE}: {e}")

    def flush_pending_save(self) -> None:
        """Дописывает accounts.json, если изменения были отложены дебаунсом."""
        with self._save_lock:
            pending = self._save_pending
        if pending:
            self.save_accounts(force=True)

    def notify_users(self, text: str):
        if hasattr(self, "tg_controller") and self.tg_controller:
            try:
                self.tg_controller.broadcast(text)
            except Exception as e:
                print(f"[!] Ошибка отправки уведомления в TG: {e}")

    def add_bot(
        self,
        target_server: str,
        nickname: str,
        password: str,
        auto_regen_nick: bool = False,
        default_task: str = "lvl",
        proxy: str = "",
    ) -> BotInstance | None:
        if not password or not password.strip():
            print("[-] Ошибка: Пароль обязателен для каждого аккаунта!")
            return None

        # Ключ из пула («HAPP») превращаем в socks5://... до создания бота,
        # иначе в accounts.json уедет имя вместо адреса.
        if proxy:
            resolved = self.resolve_proxy(proxy)
            if not resolved:
                print(f"[-] Прокси не годится: {proxy}. Бот пойдёт со своего IP.")
            proxy = resolved

        password = password.strip()

        try:
            srv = br_servers.find_server(target_server)
        except Exception as e:
            print(f"[!] Ошибка загрузки списка серверов: {e}")
            return None

        if not srv:
            print(f"[-] Сервер '{target_server}' не найден.")
            return None

        ip = srv["ip"]
        port = int(srv["port"])
        clean_name = srv.get("firstname") or srv.get("name", "").replace("BLACK RUSSIA | ", "").strip()
        clean_name = clean_name.upper()

        with self._lock:
            bot_id = self._next_id
            self._next_id += 1
            bot = BotInstance(
                bot_id=bot_id,
                host=ip,
                port=port,
                nickname=nickname,
                password=password,
                server_name=clean_name,
                auto_regen_nick=auto_regen_nick,
                manager=self,
                is_banned=False,
                default_task=default_task,
                proxy=proxy,
            )
            self.bots[bot_id] = bot
            if self.active_bot_id is None:
                self.active_bot_id = bot_id
            self._nick_index[bot.nickname] = bot

        self.save_accounts(force=True)
        bot.start()
        if default_task:
            bot.set_task(default_task)

        px_label = f" [Прокси: {proxy}]" if proxy else " [Свой IP]"
        print(f"[+] Бот #{bot_id} ({nickname}) запущен на сервере {clean_name} ({ip}:{port}){px_label}")
        return bot

    def generate_random_nickname(self) -> str:
        names_file = os.path.join(os.path.dirname(__file__), "names.txt")
        surnames_file = os.path.join(os.path.dirname(__file__), "surnames.txt")

        if not hasattr(self, "_cached_names") or not self._cached_names:
            self._cached_names = []
            if os.path.exists(names_file):
                with open(names_file, "r", encoding="utf-8", errors="ignore") as f:
                    self._cached_names = [line.strip() for line in f if line.strip()]
            if not self._cached_names:
                self._cached_names = ["Alex", "Dmitry", "Sergey", "Maxim", "Ivan", "Artem", "Nikita"]

        if not hasattr(self, "_cached_surnames") or not self._cached_surnames:
            self._cached_surnames = []
            if os.path.exists(surnames_file):
                with open(surnames_file, "r", encoding="utf-8", errors="ignore") as f:
                    self._cached_surnames = [line.strip() for line in f if line.strip()]
            if not self._cached_surnames:
                self._cached_surnames = ["Smith", "Ivanov", "Smirnov", "Kuznetsov", "Popov", "Sokolov"]

        if not hasattr(self, "_used_nicknames"):
            self._used_nicknames = set()

        with self._lock:
            current_active_nicks = {b.nickname for b in self.bots.values() if b.nickname}
            all_used = self._used_nicknames | current_active_nicks

        for _ in range(500):
            first_name = random.choice(self._cached_names)
            last_name = random.choice(self._cached_surnames)
            nick = f"{first_name}_{last_name}"
            if len(nick) > MAX_NICKNAME_LEN:
                nick = nick[:MAX_NICKNAME_LEN]
            if nick not in all_used:
                self._used_nicknames.add(nick)
                return nick

        suffix = random.randint(10, 99)
        base = f"{random.choice(self._cached_names)}_{random.choice(self._cached_surnames)}"
        nick = f"{base[:MAX_NICKNAME_LEN - 3]}_{suffix}"
        self._used_nicknames.add(nick)
        return nick

    def replace_bot_nickname(self, bot: BotInstance):
        if bot.stopping:
            return

        old_nick = bot.nickname
        new_nick = self.generate_random_nickname()
        bot.add_log(f"Ник {old_nick} занят/зарегистрирован! Новый ник: {new_nick}")
        print(f"[!] Бот #{bot.bot_id} [{old_nick}] -> ник занят. Замена на: {new_nick}")

        task_name = bot.default_task_name or (bot.active_task.name if bot.active_task else "lvl")
        srv_name = bot.server_name
        pwd = bot.password

        bot.stop()

        def _respawn():
            time.sleep(2.0)
            with self._lock:
                if bot.bot_id in self.bots:
                    del self.bots[bot.bot_id]
                self._forget_bot(bot)

            new_b = self.add_bot(srv_name, new_nick, password=pwd, auto_regen_nick=True, default_task=task_name)
            self.save_accounts(force=True)

        threading.Thread(target=_respawn, daemon=True).start()

    def load_proxies_list(self) -> list[str]:
        """Загрузка списка прокси из proxies.txt (формат ip:port или ip:port:user:pass или socks5://...)"""
        if not os.path.exists(PROXIES_FILE):
            return []
        try:
            with open(PROXIES_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
            return lines
        except Exception:
            return []

    def add_bots_batch(
        self,
        target_server: str,
        count: int,
        password: str,
        default_task: str = "lvl",
        proxies: list[str] | None = None,
    ):
        if not password or not password.strip():
            print("[-] Ошибка: Пароль обязателен для создания ботов!")
            return []

        password = password.strip()
        created = []
        proxies_pool = list(proxies) if proxies else []
        if proxies_pool:
            # Проверяем прокси один раз на всю пачку: незачем дёргать сервер
            # N раз за одно и то же, а мёртвые надо выкинуть до захода.
            working = []
            for item in proxies_pool:
                resolved = self.resolve_proxy(item)
                if not resolved:
                    print(f"[-] Прокси не годится, пропущен: {item}")
                    continue
                pool = getattr(self, "proxy_pool", None)
                if pool is not None:
                    ok, reason = pool.verify_before_connect(resolved)
                    if not ok:
                        print(f"[-] Прокси мёртв, пропущен: {item} ({reason})")
                        continue
                working.append(resolved)
            proxies_pool = working
            if not proxies_pool:
                print("[-] Ни один прокси не прошёл проверку — создаю без прокси.")

        for i in range(count):
            nick = self.generate_random_nickname()
            # Если переданы прокси, распределяем их (по кругу)
            assigned_proxy = ""
            if proxies_pool:
                assigned_proxy = proxies_pool[i % len(proxies_pool)]

            bot = self.add_bot(
                target_server,
                nick,
                password=password,
                auto_regen_nick=True,
                default_task=default_task,
                proxy=assigned_proxy,
            )
            if bot:
                created.append(bot)
            time.sleep(2.0)
        print(f"[+] Успешно создано и запущено {len(created)} ботов на сервере '{target_server}' с задачей '{default_task}'")
        return created

    def start_selected(self, bot_ids: list[int]):
        with self._lock:
            bots_to_start = [
                self.bots[bid] for bid in bot_ids
                if bid in self.bots and not self.bots[bid].is_banned and "В игре" not in self.bots[bid].status and "Подключение" not in self.bots[bid].status
            ]

        if not bots_to_start:
            print("[*] Нет выбранных ботов, готовых к запуску.")
            return []

        def _starter():
            for b in bots_to_start:
                if b.is_banned or b.stopping:
                    continue
                print(f"[+] Запуск бота #{b.bot_id} [{b.nickname}] на сервере {b.server_name}...")
                b.start()
                time.sleep(0.4)
            print(f"[+] Выбранные боты ({len(bots_to_start)}) запущены!")

        threading.Thread(target=_starter, daemon=True).start()
        return bots_to_start

    def start_all(self):
        with self._lock:
            bots_to_start = [
                b for b in self.bots.values()
                if not b.is_banned and "В игре" not in b.status and "Подключение" not in b.status
            ]

        if not bots_to_start:
            print("[*] Нет ботов, готовых к запуску (все уже в игре или забанены).")
            return

        print(f"[*] Запуск {len(bots_to_start)} ботов...")

        def _starter():
            for b in bots_to_start:
                if b.is_banned or b.stopping:
                    continue
                print(f"[+] Запуск бота #{b.bot_id} [{b.nickname}] на сервере {b.server_name}...")
                b.start()
                # Небольшая пауза между запусками ботов (0.3-0.5с), чтобы не вызывать лавину UDP
                time.sleep(0.4)
            print(f"[+] Все боты ({len(bots_to_start)}) запущены!")

        threading.Thread(target=_starter, daemon=True).start()

    def start_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        if bot.is_banned:
            print(f"[!] Бот #{bot_id} помечен как ЗАБАНЕН. Используйте /unban {bot_id} если бан снят.")
            return
        bot.start()
        print(f"[+] Бот #{bot_id} [{bot.nickname}] запускается...")

    def unban_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        bot.is_banned = False
        bot.status = "Отключен"
        bot.add_log("Статус бана снят пользователем")
        self.save_accounts()
        print(f"[+] Бот #{bot_id} [{bot.nickname}] разбанен! Теперь его можно запустить командой /start {bot_id}")

    def resolve_proxy(self, proxy_str: str) -> str:
        """Ключ из пула или голая строка -> строка для bot.proxy.

        Пусто, если прокси не годится: мёртвый узел или узел, которому нужен
        внешний клиент. Возвращать «как есть» нельзя — бот уйдёт в коннект
        с заведомо нерабочим прокси.
        """
        proxy_clean = (proxy_str or "").strip()
        if not proxy_clean:
            return ""
        if proxy_clean.lower() in ("none", "0", "off", "no", "direct", "-"):
            return ""
        # Ссылка уже готова — берём как есть
        if "://" in proxy_clean:
            if proxy_clean.startswith("http://"):
                return "socks5://" + proxy_clean[7:]
            if proxy_clean.startswith("https://"):
                return "socks5://" + proxy_clean[8:]
            return proxy_clean
        # Иначе это ключ из пула
        pool = getattr(self, "proxy_pool", None)
        if pool is None:
            return proxy_clean
        node = pool.find_node(proxy_clean)
        if node is None:
            return proxy_clean
        if not node.is_direct:
            return ""
        return node.proxy_url()

    def set_bot_proxy(self, bot_id: int, proxy_str: str) -> bool:
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                return False
            raw = (proxy_str or "").strip()
            if raw.lower() in ("none", "0", "off", "no", "direct", "-"):
                bot.proxy = ""
            else:
                resolved = self.resolve_proxy(raw)
                if not resolved:
                    return False
                bot.proxy = resolved

        self.save_accounts()
        return True

    def apply_proxy_to_bots(self, proxy_str: str, bot_ids: list[int] | None = None) -> dict:
        """Раздать прокси пачке ботов. Пустой список — всем.

        Сначала проверка прокси, потом раздача: если прокси мёртв, ни одному
        боту он не достанется.
        """
        pool = getattr(self, "proxy_pool", None)
        resolved = self.resolve_proxy(proxy_str)
        if not resolved:
            return {"ok": False, "reason": "прокси не годится", "applied": []}

        if pool is not None:
            ok, reason = pool.verify_before_connect(resolved)
            if not ok:
                return {"ok": False, "reason": reason, "applied": []}

        with self._lock:
            targets = (
                [self.bots[bid] for bid in bot_ids if bid in self.bots]
                if bot_ids
                else list(self.bots.values())
            )
            for bot in targets:
                bot.proxy = resolved
            applied = [bot.bot_id for bot in targets]

        self.save_accounts()
        return {"ok": True, "reason": resolved, "applied": applied}

    def proxy_report(self) -> str:
        """Текст отчёта по прокси для консоли и Telegram."""
        pool = getattr(self, "proxy_pool", None)
        if pool is None:
            return "Пул прокси не запущен."
        statuses = pool.statuses()
        if not statuses:
            summary = pool.summary()
            if summary["subscriptions"] or summary["manual"]:
                return (
                    f"Узлы есть ({summary['subscriptions']} подписок, "
                    f"{summary['manual']} ручных), но ещё не проверены. "
                    "Запусти /vpn ping"
                )
            return (
                "Прокси нет. Добавь VPN-подписку или локальный прокси:\n"
                "  /vpn add <ссылка>\n"
                "  /vpn local <имя> socks5://host:port"
            )
        summary = pool.summary()
        lines = [
            f"Прокси: {summary['usable']} рабочих из {summary['total']} "
            f"(обновлено {time.strftime('%H:%M:%S', time.localtime(summary['last_refresh']))})",
            "",
        ]
        for res in statuses:
            node = res.node
            if res.usable:
                mark = "🟢"
            elif res.alive:
                mark = "🟡"
            else:
                mark = "🔴"
            latency = f"{res.latency_ms:.0f}мс" if res.latency_ms else "—"
            note = res.reason or "ок"
            lines.append(
                f"{mark} {node.name} — {node.address}:{node.port} "
                f"[{node.protocol}] {latency} · {note}"
            )
        return "\n".join(lines)

    def delete_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                print(f"[-] Бот #{bot_id} не найден.")
                return
            bot.stop()
            del self.bots[bot_id]
            self._forget_bot(bot)
        self.save_accounts(force=True)
        print(f"[*] Бот #{bot_id} [{bot.nickname}] удален из списка и accounts.json.")

    def _get_bot_location_name(self, bot: BotInstance) -> str:
        """Определяет понятное название текущей локации бота по координатам"""
        if not bot.api or "В игре" not in bot.status:
            return ""
        try:
            pos = bot.api.getbotposition()
            if not pos or len(pos) < 3 or pos[0] == 0.0:
                return "Спавн"
            x, y, z = pos[0], pos[1], pos[2]

            # База координат локаций области Black Russia
            locations = [
                ("Спавн Батырево", 1795.375, 2518.425, 120.0),
                ("Территория шахты", 2378.989, 1670.212, 80.0),
                ("Вход в шахту", 2379.218, 1677.226, 30.0),
                ("Переодевание в шахте", 540.554, 515.490, 20.0),
                ("Добыча руды", 542.027, 535.701, 20.0),
                ("Сдача руды", 557.287, 516.363, 20.0),
                ("Спавн Лыткарино", -2428.754, 203.862, 120.0),
                ("Спавн Южный", 2745.431, -2436.016, 120.0),
                ("Спавн Нижегородск 1", -2661.548, 2000.877, 120.0),
                ("Спавн Нижегородск 2", -2152.833, 1558.348, 120.0),
                ("Место для АФК", 1549.989, 2717.540, 60.0),
                ("Банк Арзамаса", 2280.0, -1450.0, 60.0),
                ("Вокзал Арзамас", 2125.0, -1145.0, 180.0),
                ("Центр Арзамаса", 2250.0, -1350.0, 250.0),
                ("Больница Арзамас", 2040.0, -1410.0, 150.0),
                ("Вокзал Батырево", 1060.0, 1260.0, 150.0),
                ("Военкомат / Батырево", 1100.0, 1340.0, 200.0),
                ("Автошкола Лыткарино", 2065.0, -2385.0, 120.0),
                ("Вокзал Лыткарино", 2800.0, -2320.0, 180.0),
                ("г. Нижегородск", 2850.0, 500.0, 400.0),
                ("Вокзал Южный", -2430.0, 2350.0, 180.0),
                ("ГИБДД Южный", -2550.0, 2280.0, 200.0),
                ("Ферма", 120.0, 250.0, 220.0),
                ("Эдово", 2300.0, 2400.0, 300.0),
                ("Бусаево", 1550.0, 350.0, 250.0),
                ("Корякино", 1850.0, -450.0, 250.0),
            ]

            for name, lx, ly, radius in locations:
                dist = ((x - lx)**2 + (y - ly)**2)**0.5
                if dist <= radius:
                    return name

            # Если конкретной точки нет, определяем по регионам карты
            if x > 1500 and y < -800 and y > -1800:
                return "г. Арзамас"
            elif x > 1700 and y < -2000:
                return "г. Лыткарино"
            elif x > 600 and x < 1400 and y > 600 and y < 1600:
                return "пгт. Батырево"
            elif x < -1800 and y > 1800:
                return "г. Южный"
            elif x > 1800 and y > 1800:
                return "пгт. Эдово"

            return f"Карта ({int(x)}, {int(y)})"
        except Exception:
            return "В игре"

    def list_bots(self):
        print("\n" + "=" * 124)
        print(f"{'ID':<4} {'Никнейм':<18} {'Сервер':<10} {'ЛВЛ':<12} {'Задача':<13} {'Локация':<20} {'Статус':<22} {'Прокси':<15}")
        print("-" * 124)
        with self._lock:
            if not self.bots:
                print("   (нет добавленных ботов)")
            for bid, b in self.bots.items():
                cur_marker = "* " if bid == self.active_bot_id else "  "
                online_str = b.status
                if b.connected_time and "В игре" in b.status:
                    uptime = int(time.time() - b.connected_time)
                    m, s = divmod(uptime, 60)
                    h, m = divmod(m, 60)
                    online_str += f" ({h:02d}:{m:02d})"
                task_name = b.active_task.name if b.active_task else (b.default_task_name or "Ручной")
                # Укорачиваем длинные названия задач
                short_task = task_name.split()[0] if task_name else "Ручной"
                if "качка" in task_name.lower():
                    short_task = "Качка LVL"
                elif "шахт" in task_name.lower():
                    short_task = "Шахтер"
                elif "завод" in task_name.lower():
                    short_task = "Завод"
                elif "автобус" in task_name.lower():
                    short_task = "Автобусник"
                elif "квест" in task_name.lower():
                    short_task = "Дядя Слава"

                loc_name = self._get_bot_location_name(b) or ("-" if "В игре" not in b.status else "Загрузка...")
                lvl_disp = b.level_str or "-"
                px_disp = b.proxy.split("@")[-1] if b.proxy else "Свой IP"
                if len(px_disp) > 14:
                    px_disp = px_disp[:12] + ".."
                print(f"{cur_marker}{bid:<2} {b.nickname:<18} {b.server_name[:9]:<10} {lvl_disp:<12} {short_task:<13} {loc_name:<20} {online_str:<22} {px_disp:<15}")
        print("=" * 124)
        if self.active_bot_id is not None:
            active_bot = self.bots.get(self.active_bot_id)
            if active_bot:
                print(f"[*] Активный бот для команд: #{self.active_bot_id} [{active_bot.nickname}]\n")

    def show_logs(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return

        print(f"\n--- Последние логи бота #{bot_id} [{bot.nickname}] ({len(bot.logs)} записей) ---")
        if not bot.logs:
            print("   (логов пока нет)")
        else:
            for line in list(bot.logs):
                print(line)
        print("--- Конец логов ---\n")

    def switch_active(self, bot_id: int):
        with self._lock:
            if bot_id not in self.bots:
                print(f"[-] Бот #{bot_id} не существует.")
                return False
            self.active_bot_id = bot_id
            bot = self.bots[bot_id]
        print(f"[+] Выбран бот #{bot_id} [{bot.nickname}] на сервере {bot.server_name}")
        return True

    def stop_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        bot.stop()
        print(f"[*] Бот #{bot_id} [{bot.nickname}] остановлен.")

    def stop_all(self):
        with self._lock:
            bots_list = list(self.bots.values())
        for b in bots_list:
            b.stop()


def print_help():
    print("""
====================== УПРАВЛЕНИЕ МУЛЬТИБОТОМ ======================
  /menu             - Главное меню управления аккаунтами и задачами
  /list             - Список всех ботов, серверов, паролей и статусов
  /startall         - Запустить всех доступных ботов (Кнопка 'Старт всех')
  /start <ID|1,2,3> - Запустить бота или нескольких через запятую
  /stopall          - Остановить всех ботов
  /stop <ID|1,2,3>  - Остановить бота или нескольких через запятую
  /batch <сервер> <кол-во> <пароль> [прокси] - Создать пачку ботов с авто-никами
  /add <сервер> <ник> <пароль> [прокси]      - Добавить аккаунт вручную
  /vpn                              - Прокси и VPN-подписки: отчёт
  /vpn add [имя] <ссылка>           - Добавить VPN-подписку
  /vpn local <имя> <socks5://...>   - Добавить прокси вручную
  /vpn ping                         - Перепроверить все узлы сейчас
  /vpn use <узел> [ID|all]          - Применить прокси к ботам
  /proxy <ID|all> <узел|none>       - Прокси конкретному боту
  /task <lvl|quest|mine|factory|bus|stop> [ID] - Назначить задачу боту
  /switch <ID>      - Выбрать активного бота для консольных команд
  /logs [ID]        - Посмотреть последние логи бота
  /unban <ID>       - Снять статус бана с аккаунта
  /del <ID>         - Удалить бота из списка и accounts.json
  /stream           - Вкл/выкл живой вывод логов активного бота в консоль
  /q                - Выход и остановка всех ботов

--- Команды для активного бота ---
  /pos              - Показать текущие координаты X, Y, Z бота
  /goto <X> <Y> <Z> [mode] - Отправить бота на координаты (walk/run/sprint)
  /tpnext [скорость] - Доехать до ближайшего ДРУГОГО вокзала (8.5 м/с)
  /tpspawn <имя>    - Доехать до конкретного вокзала
  /spawn            - Переспавниться на вокзал
  /chat <текст>     - Отправить сообщение в чат
  /<команда>        - Любая серверная команда (например /time, /stats, /mm)
  /dialog <id> <кнопка> [список] [текст] - Ответить в диалог
====================================================================
""")


def interactive_menu(manager: BotManager):
    manager.stream_active_logs = False
    try:
        while True:
            print("\n============ МЕНЮ УПРАВЛЕНИЯ ============")
            print("1. Показать список всех аккаунтов")
            print("2. Добавить один аккаунт вручную (Пароль обязателен)")
            print("3. Создать пачку ботов (Авто-генерация ников + качка LVL)")
            print("4. ЗАПУСТИТЬ ВСЕХ БОТОВ (Кнопка 'Старт всех')")
            print("5. Остановить всех ботов")
            print("6. Запустить режим бота (Качка LVL / Квесты / Шахта)")
            print("7. Посмотреть логи аккаунта")
            print("8. Переключить активный аккаунт")
            print("9. Остановить одного бота")
            print("10. Снять статус бана с аккаунта")
            print("11. Удалить аккаунт из списка")
            print("12. Управление прокси (добавить / посмотреть / привязать)")
            print("0. Вернуться в консоль")
            print("=========================================")
            choice = input("Выберите пункт (0-12): ").strip()

            if choice == "0":
                break
            elif choice == "1":
                manager.list_bots()
            elif choice == "2":
                srv = input("Сервер (название или номер, например tver): ").strip()
                if not srv:
                    print("Отменено.")
                    continue
                nick = input("Никнейм (Name_Surname): ").strip()
                if not nick:
                    print("[-] Ошибка: Ник не может быть пустым.")
                    continue
                while True:
                    pwd = input("Пароль аккаунта (обязательно): ").strip()
                    if pwd:
                        break
                    print("[-] Ошибка: Пароль обязателен!")
                px_choice = input("Прокси (Enter для собственного IP, или ip:port / ip:port:user:pass): ").strip()
                manager.add_bot(srv, nick, pwd, proxy=px_choice)
            elif choice == "3":
                srv = input("Сервер (название или номер, например tver): ").strip()
                if not srv:
                    print("Отменено.")
                    continue
                cnt_str = input("Количество ботов: ").strip()
                if not cnt_str.isdigit() or int(cnt_str) <= 0:
                    print("[-] Ошибка: Неверное количество.")
                    continue
                count = int(cnt_str)
                while True:
                    pwd = input("Пароль для всех создаваемых аккаунтов (обязательно): ").strip()
                    if pwd:
                        break
                    print("[-] Ошибка: Пароль обязателен!")
                
                # Выбор прокси для пачки
                px_list = manager.load_proxies_list()
                selected_proxies = None
                if px_list:
                    print(f"\n[i] Найдено {len(px_list)} прокси в {os.path.basename(PROXIES_FILE)}.")
                    use_px = input(f"Использовать прокси из файла для распределения? (y/n, по умолч. y): ").strip().lower()
                    if use_px != "n":
                        selected_proxies = px_list
                else:
                    manual_px = input("Прокси для пачки (Enter - свой IP, или введите через запятую/один прокси): ").strip()
                    if manual_px:
                        selected_proxies = [p.strip() for p in manual_px.split(",") if p.strip()]

                manager.add_bots_batch(srv, count, password=pwd, default_task="lvl", proxies=selected_proxies)
            elif choice == "4":
                manager.start_all()
            elif choice == "5":
                manager.stop_all()
                print("[*] Все боты остановлены.")
            elif choice == "6":
                manager.list_bots()
                b_str = input("Введите ID бота для смены режима: ").strip()
                if b_str.isdigit():
                    bid = int(b_str)
                    with manager._lock:
                        target_b = manager.bots.get(bid)
                    if not target_b:
                        print(f"[-] Бот #{bid} не найден.")
                        continue
                    print("\nВыберите режим:")
                    print("  1. Качка LVL (Anti-AFK, микро-движения, сбор наград, скип диалогов)")
                    print("  2. Квесты Дяди Славы (авто-диалоги, маркеры, посадка в авто)")
                    print("  3. Бот Шахтер (аренда скутера, добыча руды с ползунком, склад)")
                    print("  4. Бот Завод (аренда скутера, сырье, станок с ползунком, авто-перезабор при браке)")
                    print("  5. Бот Автобусник (проверка/сдача на кат. D, рейс в Арзамасе, бессмертие, объезд)")
                    print("  6. Выключить авто-режим (Ручное управление)")
                    t_choice = input("Режим (1-6): ").strip()
                    if t_choice == "1":
                        res = target_b.set_task("lvl")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "2":
                        res = target_b.set_task("quest")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "3":
                        res = target_b.set_task("mine")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "4":
                        res = target_b.set_task("factory")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "5":
                        res = target_b.set_task("bus")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "6":
                        res = target_b.set_task("stop")
                        print(f"[*] Бот #{bid}: {res}")
            elif choice == "7":
                manager.list_bots()
                b_str = input("Введите ID бота для просмотра логов: ").strip()
                if b_str.isdigit():
                    manager.show_logs(int(b_str))
            elif choice == "8":
                manager.list_bots()
                b_str = input("Введите ID бота, на которого переключиться: ").strip()
                if b_str.isdigit():
                    manager.switch_active(int(b_str))
            elif choice == "9":
                manager.list_bots()
                b_str = input("Введите ID бота для остановки: ").strip()
                if b_str.isdigit():
                    manager.stop_bot(int(b_str))
            elif choice == "10":
                manager.list_bots()
                b_str = input("Введите ID бота для снятия бана: ").strip()
                if b_str.isdigit():
                    manager.unban_bot(int(b_str))
            elif choice == "11":
                manager.list_bots()
                b_str = input("Введите ID бота для удаления: ").strip()
                if b_str.isdigit():
                    manager.delete_bot(int(b_str))
            elif choice == "12":
                print("\n--- УПРАВЛЕНИЕ ПРОКСИ ---")
                print("1. Посмотреть текущие прокси из proxies.txt")
                print("2. Добавить прокси в proxies.txt")
                print("3. Привязать / изменить прокси у бота")
                p_sub = input("Выберите (1-3): ").strip()
                if p_sub == "1":
                    cur_px = manager.load_proxies_list()
                    if cur_px:
                        print(f"Список прокси ({len(cur_px)}):")
                        for idx, px in enumerate(cur_px, 1):
                            print(f"  {idx}. {px}")
                    else:
                        print(f"Файл {os.path.basename(PROXIES_FILE)} пуст или не существует.")
                elif p_sub == "2":
                    print("Формат: ip:port или ip:port:user:pass (или socks5://...)")
                    new_p = input("Введите прокси: ").strip()
                    if new_p:
                        with open(PROXIES_FILE, "a", encoding="utf-8") as pf:
                            pf.write(new_p + "\n")
                        print("[+] Прокси сохранен в proxies.txt")
                elif p_sub == "3":
                    manager.list_bots()
                    b_str = input("Введите ID бота: ").strip()
                    if b_str.isdigit():
                        bid = int(b_str)
                        with manager._lock:
                            target_b = manager.bots.get(bid)
                        if target_b:
                            new_proxy = input(f"Введите новый прокси для #{bid} (или 'none'/'0' для сброса на свой IP): ").strip()
                            if new_proxy.lower() in ("none", "0", "off", "no"):
                                target_b.proxy = ""
                                print(f"[+] У бота #{bid} сброшен прокси на собственный IP.")
                            else:
                                target_b.proxy = new_proxy
                                print(f"[+] Боту #{bid} назначен прокси: {new_proxy}")
                            manager.save_accounts()
                        else:
                            print(f"[-] Бот #{bid} не найден.")
    finally:
        manager.stream_active_logs = True
        print("[*] Вы вернулись в консоль управления. Введите /help для подсказки.")


def _parse_bot_ids(text: str, manager) -> list[int] | None:
    """`1,2,5` или `all` -> список id. None — значит «все»."""
    text = (text or "").strip().lower()
    if not text or text in ("all", "все", "*"):
        return None
    ids = [int(part) for part in text.split(",") if part.strip().isdigit()]
    return ids or None


def handle_vpn_command(manager, line: str) -> None:
    """Команды пула прокси и VPN-подписок."""
    parts = line.split()
    pool = getattr(manager, "proxy_pool", None)
    if pool is None:
        print("[-] Пул прокси не запущен.")
        return

    action = parts[1].lower() if len(parts) > 1 else ""

    if action in ("", "list", "status"):
        print(manager.proxy_report())
        summary = pool.summary()
        print(
            f"\n[i] Подписок: {summary['subscriptions']} | "
            f"ручных: {summary['manual']} | обновление каждые "
            f"{summary['interval']:.0f}с"
        )
        if summary["last_error"]:
            print(f"[!] Последняя ошибка: {summary['last_error']}")
        return

    if action == "add":
        rest = parts[2:]
        if not rest:
            print("Использование: /vpn add [имя] <ссылка>")
            return
        if len(rest) == 1:
            name, url = "", rest[0]
        else:
            name, url = rest[0], rest[1]
        print(f"[*] Тяну подписку {url} ...")
        try:
            nodes = pool.add_subscription(url, name=name)
        except Exception as exc:
            print(f"[-] Не вышло: {exc}")
            return
        print(f"[+] Узлов в подписке: {len(nodes)}. Проверяю...")
        pool.refresh(fetch=False)
        print(manager.proxy_report())
        return

    if action in ("local", "manual"):
        rest = parts[2:]
        if len(rest) < 2:
            print("Использование: /vpn local <имя> socks5://host:port")
            return
        name, proxy = rest[0], rest[1]
        try:
            pool.add_manual(proxy, name=name)
        except Exception as exc:
            print(f"[-] Не вышло: {exc}")
            return
        ok, reason = pool.verify_before_connect(proxy)
        if ok:
            print(f"[+] {name}: {reason}")
        else:
            print(f"[!] {name} добавлен, но проверка не прошла: {reason}")
        return

    if action in ("ping", "check", "refresh"):
        print("[*] Проверяю все узлы...")
        results = pool.refresh()
        if not results:
            print("[-] Нечего проверять: нет узлов.")
            return
        print(manager.proxy_report())
        return

    if action in ("del", "remove", "rm"):
        if len(parts) < 3:
            print("Использование: /vpn del <имя|ссылка|прокси>")
            return
        needle = parts[2]
        if pool.remove_subscription(needle):
            print(f"[+] Подписка удалена: {needle}")
        elif pool.remove_manual(needle):
            print(f"[+] Ручной прокси удалён: {needle}")
        else:
            print(f"[-] Не нашла: {needle}")
        return

    if action == "use":
        if len(parts) < 3:
            print("Использование: /vpn use <ключ|имя|адрес> [id,id|all]")
            return
        needle = parts[2]
        bot_ids = _parse_bot_ids(parts[3], manager) if len(parts) > 3 else None
        found = pool.find(needle)
        if found is None:
            print(f"[-] Узел не найден: {needle}")
            return
        if not found.node.is_direct:
            print(
                f"[-] {found.node.protocol.upper()} боту не годится: "
                "нужен внешний клиент, бот умеет socks5/http."
            )
            return
        if not found.usable:
            print(f"[-] Узел не работает: {found.reason or 'проверка не прошла'}")
            return
        report = manager.apply_proxy_to_bots(found.node.proxy_url(), bot_ids)
        if not report["ok"]:
            print(f"[-] Не применила: {report['reason']}")
            return
        print(
            f"[+] Прокси {found.node.name} применён к "
            f"{len(report['applied'])} ботам: {report['applied']}"
        )
        return

    if action in ("auto", "spread"):
        working = pool.usable_proxies()
        if not working:
            print("[-] Нет рабочих прокси. Сначала /vpn ping.")
            return
        with manager._lock:
            targets = list(manager.bots.values())
        if not targets:
            print("[-] Ботов нет.")
            return
        for index, bot in enumerate(targets):
            bot.proxy = working[index % len(working)]
        manager.save_accounts()
        print(
            f"[+] Роздано {len(working)} прокси на {len(targets)} ботов "
            f"(по кругу)."
        )
        return

    print(
        "Команды VPN:\n"
        "  /vpn                      отчёт по прокси\n"
        "  /vpn add [имя] <ссылка>   добавить подписку\n"
        "  /vpn local <имя> <прокси> добавить socks5 вручную\n"
        "  /vpn ping                 перепроверить сейчас\n"
        "  /vpn del <имя>            удалить подписку или ручной\n"
        "  /vpn use <узел> [ids]     применить к ботам\n"
        "  /vpn auto                 раздать рабочие по кругу"
    )


def handle_proxy_command(manager, line: str) -> None:
    """/proxy — прокси конкретного бота или всех."""
    parts = line.split(maxsplit=2)
    if len(parts) < 3:
        print("Использование: /proxy <ID|all> <ключ|прокси|none>")
        return
    target, value = parts[1], parts[2]
    if target.lower() in ("all", "все", "*"):
        report = manager.apply_proxy_to_bots(value, None)
        if not report["ok"]:
            print(f"[-] Не применила: {report['reason']}")
            return
        print(f"[+] Прокси применён к {len(report['applied'])} ботам.")
        return
    if not target.isdigit():
        print("Использование: /proxy <ID|all> <ключ|прокси|none>")
        return
    bid = int(target)
    if manager.set_bot_proxy(bid, value):
        bot = manager.bots.get(bid)
        current = bot.proxy if bot else ""
        print(f"[+] Бот #{bid}: прокси = {current or 'свой IP'}")
    else:
        print(f"[-] Не вышло: бот #{bid} не найден или прокси не годится.")


def main():
    print("=" * 70)
    print("   Black Russia Headless RakSAMP Bot Client [Multi-Account]   ")
    print("=" * 70)

    # Разбор аргументов для Telegram Bot
    tg_token = None
    tg_admin = None
    filtered_args = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--tg", "--telegram", "--token") and i + 1 < len(sys.argv):
            tg_token = sys.argv[i + 1]
            i += 2
        elif arg.startswith("--tg="):
            tg_token = arg.split("=", 1)[1]
            i += 1
        elif arg in ("--admin", "--tg-admin") and i + 1 < len(sys.argv):
            try:
                tg_admin = int(sys.argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif arg.startswith("--admin="):
            try:
                tg_admin = int(arg.split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        else:
            filtered_args.append(arg)
            i += 1

    # Также проверяем переменную окружения TG_BOT_TOKEN
    if not tg_token:
        tg_token = os.environ.get("TG_BOT_TOKEN")
    if not tg_admin and os.environ.get("TG_ADMIN_ID"):
        try:
            tg_admin = int(os.environ.get("TG_ADMIN_ID"))
        except ValueError:
            pass

    manager = BotManager()

    if tg_token:
        print(f"[*] Запуск Telegram бота управления...")
        tg_bot = TelegramBotController(tg_token, manager, admin_id=tg_admin)
        manager.tg_controller = tg_bot
        tg_bot.start()

    if len(filtered_args) >= 3:
        target_server = filtered_args[0]
        nickname = filtered_args[1]
        password = filtered_args[2]
        manager.add_bot(target_server, nickname, password=password)
    elif len(filtered_args) in (1, 2):
        print("[-] Ошибка: Для запуска через аргументы пароль обязателен!")
        print("Использование: python3 bot.py <сервер> <никнейм> <пароль> [--tg ТОКЕН] [--admin ID]")
    else:
        if manager.bots:
            print(f"\n[i] Найдено {len(manager.bots)} сохраненных аккаунтов из {os.path.basename(ACCOUNTS_FILE)}.")
            manager.list_bots()
            print("Введите /startall чтобы запустить всех, или /menu для перехода в меню.\n")
        else:
            print("\n[i] Первый запуск. Добавьте аккаунты через меню.")
            interactive_menu(manager)

    print_help()

    # Если запущен в фоне с Telegram ботом (например nohup / daemon без интерактивного TTY)
    if tg_token and not sys.stdin.isatty():
        print("[*] Режим фоновой работы с Telegram-ботом активен. Ожидание команд из Telegram...")
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        print("\n[*] Завершение работы всех ботов...")
        manager.stop_all()
        emitter.shutdown(wait=False)
        return

    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        if line in ("/q", "exit", "quit"):
            break

        elif line in ("/menu", "menu"):
            interactive_menu(manager)

        elif line in ("/list", "list"):
            manager.list_bots()

        elif line in ("/startall", "/runall") or line == "/start all":
            manager.start_all()

        elif line.startswith("/tg"):
            parts = line.split(maxsplit=2)
            if len(parts) >= 2:
                token_val = parts[1].strip()
                admin_val = int(parts[2].strip()) if len(parts) >= 3 and parts[2].strip().isdigit() else None
                print(f"[*] Запуск Telegram-контроллера с указанным токеном...")
                try:
                    tg_bot = TelegramBotController(token_val, manager, admin_id=admin_val)
                    manager.tg_controller = tg_bot
                    tg_bot.start()
                    print(f"[+] Telegram бот успешно запущен и слушает команды!")
                except Exception as e:
                    print(f"[-] Ошибка запуска Telegram бота: {e}")
            else:
                print("Использование: /tg <ТОКЕН_БОТА> [ADMIN_ID]")

        elif line.startswith("/start"):
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                arg = parts[1].strip()
                if arg.lower() == "all":
                    manager.start_all()
                elif "," in arg:
                    # Запуск нескольких ботов через запятую, например /start 1,2,5
                    ids = []
                    for item in arg.split(","):
                        item = item.strip()
                        if item.isdigit():
                            ids.append(int(item))
                    if ids:
                        print(f"[*] Запуск выбранных ботов: {ids}...")
                        for bid in ids:
                            manager.start_bot(bid)
                    else:
                        print("[-] Не указаны корректные ID ботов через запятую.")
                elif arg.isdigit():
                    manager.start_bot(int(arg))
                else:
                    print("Использование: /start <ID бота>, /start 1,2,3 или /startall")
            else:
                print("Использование: /start <ID бота>, /start 1,2,3 или /startall")

        elif line in ("/stopall",):
            manager.stop_all()
            print("[*] Все боты остановлены.")

        elif line in ("/help", "help"):
            print_help()

        elif line.startswith("/add"):
            parts = line.split(maxsplit=4)
            if len(parts) >= 4 and parts[3].strip():
                srv = parts[1]
                nick = parts[2]
                pwd = parts[3].strip()
                px = parts[4].strip() if len(parts) >= 5 else ""
                manager.add_bot(srv, nick, pwd, proxy=px)
            else:
                print("[-] Ошибка: Пароль обязателен!")
                print("Использование: /add <сервер> <никнейм> <пароль> [прокси]")

        elif line.startswith("/switch"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.switch_active(int(parts[1]))
            else:
                print("Использование: /switch <ID бота>")

        elif line.startswith("/batch"):
            parts = line.split()
            if len(parts) >= 4 and parts[2].isdigit() and parts[3].strip():
                s_name = parts[1]
                b_count = int(parts[2])
                b_pwd = parts[3].strip()
                # Прокси можно указать один (применится ко всем) или списком
                # через запятую — тогда разойдутся по кругу.
                px_arg = parts[4].strip() if len(parts) >= 5 else ""
                px_list = [x.strip() for x in px_arg.split(",") if x.strip()]
                if px_list:
                    print(f"[*] Прокси для пачки: {len(px_list)} шт., проверяю...")
                manager.add_bots_batch(
                    s_name, b_count, password=b_pwd, default_task="lvl",
                    proxies=px_list or None,
                )
            else:
                print("[-] Ошибка: Пароль обязателен!")
                print("Использование: /batch <сервер> <кол-во> <пароль> [прокси|список]")

        elif line.startswith("/vpn"):
            handle_vpn_command(manager, line)

        elif line.startswith("/proxy"):
            handle_proxy_command(manager, line)

        elif line.startswith("/task"):
            parts = line.split()
            if len(parts) >= 2:
                t_name = parts[1].lower()
                target_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else manager.active_bot_id
                if target_id is not None:
                    with manager._lock:
                        target_b = manager.bots.get(target_id)
                    if target_b:
                        res = target_b.set_task(t_name)
                        print(f"[+] Бот #{target_id}: {res}")
                    else:
                        print(f"[-] Бот #{target_id} не найден.")
                else:
                    print("[-] Укажите ID бота: /task <lvl|quest|mine|stop> [ID]")
            else:
                print("Использование: /task <lvl|quest|mine|stop> [ID бота]")

        elif line.startswith("/logs"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.show_logs(int(parts[1]))
            elif manager.active_bot_id is not None:
                manager.show_logs(manager.active_bot_id)
            else:
                print("Нет активного бота. Укажите ID: /logs <ID>")

        elif line == "/stream":
            manager.stream_active_logs = not manager.stream_active_logs
            st = "ВКЛЮЧЕН" if manager.stream_active_logs else "ВЫКЛЮЧЕН"
            print(f"[*] Живой стриминг логов в консоль: {st}")

        elif line.startswith("/unban"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.unban_bot(int(parts[1]))
            else:
                print("Использование: /unban <ID бота>")

        elif line.startswith("/tp "):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.api:
                print("[-] Нет активного подключенного бота.")
                continue
            parts = line.split()
            if len(parts) < 4:
                print(
                    "Использование: /tp <X> <Y> <Z> [шаг_м] [интервал_с] [consistent|legit|zero]"
                    "\n  по умолчанию 0.17м за 0.02с = 8.5 м/с (безопасная скорость)"
                )
                continue
            try:
                from logic.delta_teleport import SAFE_INTERVAL, SAFE_STEP

                tx, ty, tz = float(parts[1]), float(parts[2]), float(parts[3])
                # По умолчанию — безопасная скорость: 0.17м за тик, 8.5 м/с.
                # Быстрее сервер откатывает на доверенную точку.
                step = float(parts[4]) if len(parts) > 4 else SAFE_STEP
                interval = float(parts[5]) if len(parts) > 5 else SAFE_INTERVAL
                vel_mode = parts[6] if len(parts) > 6 else "consistent"
            except ValueError:
                print("[-] Ошибка: координаты, шаг и интервал должны быть числами.")
                continue

            def _delta_tp_worker(bot_obj, target, step_m, interval_s, mode):
                from logic.delta_teleport import DeltaTeleport, DeltaTeleportError

                try:
                    bot_player = bot_obj.session.players.bot
                    engine = DeltaTeleport(
                        bot_player,
                        bot_obj.api._delta_sync_sender,
                        step=step_m,
                        interval=interval_s,
                        velocity_mode=mode,
                    )
                    plan = engine.plan(target)
                    print(
                        f"[*] Delta-телепорт #{bot_obj.bot_id}: {plan['distance']:.1f}м | "
                        f"{plan['steps']} шагов по {step_m:.2f}м | ~{plan['estimated_seconds']:.0f}с"
                    )
                    was_enabled = bool(getattr(bot_obj.session.sync, "enabled", False))
                    if was_enabled:
                        bot_obj.session.sync.stop()
                    try:
                        result = engine.run(target)
                    finally:
                        if was_enabled and bot_obj.session.connected:
                            bot_obj.session.sync.start()
                    print(
                        f"[+] Delta-телепорт: {result['status']} | шагов {result['steps']} | "
                        f"{result['elapsed']:.2f}с | ср.скорость {result['avg_speed']:.1f} м/с | "
                        f"позиция {result['final_position']}"
                    )
                    if result.get("reason"):
                        print(f"[!] Остановка: {result['reason']}")
                except (DeltaTeleportError, RuntimeError) as e:
                    print(f"[-] Delta-телепорт не запущен: {e}")

            threading.Thread(
                target=_delta_tp_worker,
                args=(active, (tx, ty, tz), step, interval, vel_mode),
                daemon=True,
            ).start()

        elif line.startswith("/tpnext"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.api:
                print("[-] Нет активного подключенного бота.")
                continue
            from logic.spawn import travel_to_nearest_station

            parts = line.split()[1:]
            try:
                speed = float(parts[0]) if parts else None
            except ValueError:
                print("[-] Скорость должна быть числом.")
                continue
            threading.Thread(
                target=lambda: print(f"[TP] {travel_to_nearest_station(active, speed=speed, log=active.add_log)}"),
                daemon=True,
            ).start()

        elif line.startswith("/tpspawn"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.api:
                print("[-] Нет активного подключенного бота.")
                continue
            from logic.delta_teleport import SPAWNS, find_spawn

            args = line.split()[1:]
            if not args:
                print("Спавны: " + ", ".join(SPAWNS))
                print("Использование: /tpspawn <имя|номер> [шаг_м] [интервал_с]")
                continue
            name, coords = find_spawn(args[0])
            if coords is None:
                print(f"[-] Спавн '{args[0]}' не найден. Доступно: {', '.join(SPAWNS)}")
                continue
            from logic.delta_teleport import SAFE_INTERVAL, SAFE_STEP

            try:
                step = float(args[1]) if len(args) > 1 else SAFE_STEP
                interval = float(args[2]) if len(args) > 2 else SAFE_INTERVAL
            except ValueError:
                print("[-] Шаг и интервал должны быть числами.")
                continue
            print(f"[*] Спавн '{name}': {coords}")
            threading.Thread(
                target=lambda: print(
                    active.api.DeltaTeleportTo(
                        coords[0], coords[1], coords[2], step=step, interval=interval
                    )
                ),
                daemon=True,
            ).start()

        elif line.startswith("/spawn"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active:
                print("[-] Нет активного бота.")
                continue
            parts = line.split()
            idx = None
            if len(parts) > 1:
                try:
                    idx = int(parts[1])
                except ValueError:
                    print("[-] Индекс вокзала должен быть числом.")
                    continue
            print(f"[*] Форсируем спавн бота #{active.bot_id} [{active.nickname}]...")
            threading.Thread(
                target=lambda: print(f"[SPAWN] {active.request_spawn(idx)}"),
                daemon=True,
            ).start()

        elif line.startswith("/spawns"):
            from logic.spawn import PROBE_INDEXES, SPAWN_INDEX, in_auth_zone
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            print(f"[*] Перебираемые индексы: {PROBE_INDEXES}")
            if SPAWN_INDEX:
                for i, (name, coords) in sorted(SPAWN_INDEX.items()):
                    print(f"    #{i} {name}: ({coords[0]:.1f}, {coords[1]:.1f}, {coords[2]:.1f})")
            else:
                print("    (маппинг ещё не выучен — заполнится после первого спавна)")
            if active and active.api:
                pos = active.api.getbotposition()
                print(f"[*] Бот #{active.bot_id}: pos={pos} | в зоне авторизации: {in_auth_zone(pos)}")

        elif line.startswith("/pos"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.api:
                print("[-] Нет активного подключенного бота.")
                continue
            pos = active.api.getbotposition()
            if pos and len(pos) >= 3:
                print(f"[+] Бот #{active.bot_id} [{active.nickname}] координаты: X={pos[0]:.2f}, Y={pos[1]:.2f}, Z={pos[2]:.2f}")
            else:
                print("[-] Координаты бота пока недоступны.")

        elif line.startswith("/goto"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.api:
                print("[-] Нет активного подключенного бота.")
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    gx, gy, gz = float(parts[1]), float(parts[2]), float(parts[3])
                    mode = parts[4] if len(parts) > 4 else "walk"
                    print(f"[*] Отправка бота #{active.bot_id} на координаты ({gx}, {gy}, {gz}) [режим: {mode}]...")
                    threading.Thread(target=lambda: active.api.MoveToCoord(gx, gy, gz, mode=mode), daemon=True).start()
                except ValueError:
                    print("[-] Ошибка: координаты должны быть числами. Пример: /goto 123.4 -567.8 12.0 walk")
            else:
                print("Использование: /goto <X> <Y> <Z> [walk/run/sprint]")

        elif line.startswith(("/del", "/delete")):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.delete_bot(int(parts[1]))
            else:
                print("Использование: /del <ID бота>")

        elif line.startswith("/stop"):
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                arg = parts[1].strip()
                if arg.lower() == "all":
                    manager.stop_all()
                    print("[*] Все боты остановлены.")
                elif "," in arg:
                    ids = []
                    for item in arg.split(","):
                        item = item.strip()
                        if item.isdigit():
                            ids.append(int(item))
                    if ids:
                        for bid in ids:
                            manager.stop_bot(bid)
                    else:
                        print("[-] Не указаны корректные ID ботов через запятую.")
                elif arg.isdigit():
                    manager.stop_bot(int(arg))
                else:
                    print("Использование: /stop <ID бота> или /stop 1,2,3")
            else:
                print("Использование: /stop <ID бота> или /stop 1,2,3")

        elif line.startswith("/dialog"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.session:
                print("[-] Нет активного подключенного бота для отправки диалога.")
                continue
            parts = line.split(maxsplit=4)
            if len(parts) >= 3:
                try:
                    d_id = int(parts[1])
                    btn = int(parts[2])
                    list_item = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                    txt = parts[4] if len(parts) > 4 else (parts[3] if len(parts) > 3 and not parts[3].isdigit() else "")
                    from samp.packets import PacketBuilder
                    active.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", d_id, btn, list_item, txt),
                        rpc_name="dialog_response",
                    )
                    active.add_log(f"Ответ в диалог #{d_id}: кнопка={btn}, элемент={list_item}, текст='{txt}'")
                except Exception as e:
                    print(f"[-] Ошибка отправки диалога: {e}")
            else:
                print("Использование: /dialog <dialog_id> <кнопка: 0/1> [номер_строки: 0] [текст]")

        elif line.startswith("/chat "):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if active:
                active.send_chat(line[6:])
            else:
                print("[-] Нет активного бота.")

        else:
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if active:
                active.send_chat(line)
            else:
                print("[-] Нет активного бота. Выберите бота через /switch <ID> или введите /menu")

    print("\n[*] Завершение работы всех ботов...")
    manager.stop_all()
    emitter.shutdown(wait=False)


if __name__ == "__main__":
    main()
