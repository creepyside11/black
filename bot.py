#!/usr/bin/env python3
# Engine: SA-MP / Black Russia RakNet | Python 3 | Termux
# Build: pip install -r requirements.txt (if any)
# Run: python3 bot.py [сервер/номер] [никнейм] [пароль]

import sys
import os
import shutil
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
from logic.baninfo import is_ip_block
from logic.registration import RegistrationConfig
from logic.json_ui import JsonUIConfig
from logic.events import emitter
from logic.bot_tasks import (LevelGrinderTask, LevelRunnerTask, QuestMasterTask,
                             MinerTask, FactoryTask, BusDriverTask)
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

# Сколько секунд считать повторное сообщение о бане тем же самым баном.
# Сервер пишет о бане дважды: строкой в чат и диалогом на входе.
BAN_ALERT_DEDUPE = 300.0

# Сторож прокси: как часто проверять, жив ли прокси у работающего бота,
# и как долго не трогать одного и того же бота после замены. Без паузы
# падение всего пула превращалось бы в шторм перезаходов — сервер на
# такое отвечает блокировкой за частые подключения.
PROXY_WATCH_INTERVAL = float(os.environ.get("BR_PROXY_WATCH", "120"))
PROXY_SWAP_COOLDOWN = float(os.environ.get("BR_PROXY_SWAP_COOLDOWN", "300"))

# Пауза между созданием аккаунтов-замен.
#
# При волне банов замена запускается на каждый забаненный аккаунт сразу.
# Раньше это давало полсотни регистраций почти одновременно — сервер
# отвечает на такое блокировкой за частые входы, и свежие аккаунты
# сгорают быстрее, чем успевают зайти. Создаём по одному с интервалом.
REPLACE_INTERVAL = max(5.0, float(os.environ.get("BR_REPLACE_INTERVAL", "25")))

# Пауза перед самой первой заменой после простоя: не создавать аккаунт в
# ту же секунду, когда пришёл бан.
REPLACE_FIRST_DELAY = (5.0, 10.0)

# Сколько раз подряд прямой вход должен провалиться, прежде чем пробовать
# прокси.
#
# На части машин игровые серверы с прямого IP недоступны: TCP-соединение
# устанавливается, а данные не идут, и UDP-хендшейк молчит. Бот при этом
# честно рапортует «Сервер не ответил на подключение» и переподключается
# вечно. Через прокси тот же сервер отвечает нормально.
DIRECT_FALLBACK_AFTER = max(1, int(os.environ.get("BR_DIRECT_FALLBACK_AFTER", "3")))

# Сколько замен разрешено за час. Защита от лавины: если сервер банит
# всё подряд, бесконечная регистрация только жжёт аккаунты.
REPLACE_PER_HOUR = max(1, int(os.environ.get("BR_REPLACE_PER_HOUR", "20")))

# Ограничение по входу (частые подключения / лимит времени).
#
# Раньше это был список подстрок вида "20 минут", и он ловил ЛЮБОЕ сообщение
# с этими словами. Первое же "Для получения зарплаты необходимо находиться
# в игре минимум 20 минут" обрывало сессию и ставило паузу 25 минут — бот
# умирал посреди телепорта на ровном месте.
#
# Теперь нужен контекст ограничения: либо явный маркер ("частые входы",
# "слишком много подключений"), либо просьба подождать/повторить со сроком.
FREQ_BAN_MARKERS = (
    "частые входы",
    "частых входов",
    "частые подключения",
    "частых подключений",
    "частые заходы",
    "слишком много подключений",
    "подозрение на спам",
    "много заходов",
)

# "подождите 20 минут", "повторите попытку через 15 мин", "попробуйте позже"
FREQ_BAN_WAIT = re.compile(
    r"(?:подожди|ожидай|повтори|попробуй|попытайся|зайди|вернись)"
    r"[^.!\n]{0,40}?"
    r"(?:(?:через\s+)?(?:\d{1,3}\s*)?(?:минут|мин\b|час)|позже)",
    re.IGNORECASE,
)

# Фразы, которые выглядят похоже, но ограничением не являются.
FREQ_BAN_NEUTRAL = (
    "зарплат",
    "начнётся игра",
    "начнется игра",
    "мероприятие",
    "розыгрыш",
    "конкурс",
)


# Диалог античита при кике.
#
# Сервер пишет «Вы были кикнуты по подозрению использования постороннего ПО»,
# а в списке триггеров стояло «постороннее по» — падеж не совпадал, и строка
# не ловилась вообще. Аккаунт не помечался баном, перезаход шёл по кругу:
# кик, 18 секунд ожидания, снова вход, снова кик.
#
# Поэтому ловим по устойчивым кускам, а не по целой фразе в именительном
# падеже: корень «посторонн» и слово «кикнут» покрывают все формулировки.
ANTICHEAT_TITLE = re.compile(r"anti[-\s]?cheat", re.IGNORECASE)
ANTICHEAT_BODY = re.compile(
    r"посторонн|кикнут|kick|чит[-\s]?программ|сторонн(?:ее|его)\s+по",
    re.IGNORECASE,
)

# Код кика из тела диалога: «Код: 25».
ANTICHEAT_CODE = re.compile(r"код\s*[:\-]?\s*(\d{1,3})", re.IGNORECASE)


def anticheat_kick(title: str, body: str) -> int | None:
    """Это кик античита? Возвращает код кика или None.

    Код нужен в причине бана: по нему видно, за что именно выкинуло.
    25 — подозрение на постороннее ПО, 2 — частые входы, и так далее.
    """
    head = re.sub(r"\{[0-9a-fA-F]{6}\}", "", str(title or ""))
    text = re.sub(r"\{[0-9a-fA-F]{6}\}", "", str(body or ""))
    if not ANTICHEAT_TITLE.search(head):
        return None
    if not ANTICHEAT_BODY.search(text):
        return None
    found = ANTICHEAT_CODE.search(text)
    return int(found.group(1)) if found else 0


def is_freq_ban_text(text: str) -> bool:
    """Это сообщение об ограничении входа, а не обычная рассылка?

    Проверяем контекст, а не отдельные слова. Иначе любое упоминание
    "20 минут" в чате считается баном.
    """
    low = re.sub(r"\{[0-9a-fA-F]{6}\}", "", str(text or "")).lower().strip()
    if not low:
        return False
    if any(word in low for word in FREQ_BAN_NEUTRAL):
        return False
    if any(marker in low for marker in FREQ_BAN_MARKERS):
        return True
    return bool(FREQ_BAN_WAIT.search(low))

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

# Минимальная пауза перед намеренным выходом (почасовой цикл payday).
# Меньше ставить нельзя: переподключение само занимает время, и бот
# вернётся раньше, чем сервер отпустит старую сессию.
PAUSE_MIN = max(1.0, float(os.environ.get("BR_PAUSE_MIN", "10")))

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


# Наборы внешности Black Russia: пол и скин подбираются парой, чтобы не
# получить мужской скин на женском персонаже.
MALE_SKINS = (78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92)
FEMALE_SKINS = (93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105)


def _appearance_for(nickname: str) -> dict:
    """Стабильная внешность из ника: один ник — всегда один персонаж."""
    seed = 0
    for ch in str(nickname or ""):
        seed = (seed * 131 + ord(ch)) & 0xFFFFFFFF
    rng = random.Random(seed)
    gender = 1 if rng.random() < 0.45 else 0
    pool = FEMALE_SKINS if gender == 1 else MALE_SKINS
    return {"gender": gender, "skin": rng.choice(pool)}


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
        proxy_node: str = "",
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
        # Кто и за что забанил. Сервер сообщает это в чате при ручном бане,
        # и без разбора строка уходила в Telegram целиком — по ней не было
        # видно ни админа, ни причины. Здесь лежит разобранный результат
        # (logic.baninfo.parse_ban).
        self.ban_admin = ""
        self.ban_reason = ""
        self.ban_kind = ""
        self.ban_duration = ""
        self.banned_at = 0.0
        self.default_task_name = default_task
        self.proxy = proxy.strip() if proxy else ""
        # Ключ узла пула под этим прокси. Нужен, чтобы после перезапуска
        # найти узел заново: порт моста не постоянный.
        self.proxy_node = str(proxy_node or "")
        # Автопереключение на прокси уже пробовали — второй раз не дёргаем.
        self._auto_proxy_tried = False

        self.level_str = "-"
        # Числа уровня отдельно от строки: из них считается «сколько до
        # следующего уровня» и полоска прогресса в панели.
        self.level_num = None
        self.exp_cur = None
        self.exp_next = None
        self.exp_left = None
        # Когда статистика реально обновилась. Панель по этому полю
        # понимает, что /mm дошёл и данные свежие, а не из реестра.
        self.stats_updated_at = 0.0
        # Время последнего сообщения в общем чате. Живой чат — доказательство,
        # что клиент уже в мире, даже если рядом никого не стримит.
        self.last_chat_at = 0.0
        self.money = 0
        self.target_lvl = None  # Целевой уровень (например 4)
        self._quest_helpers = None
        self.target_achieved = False
        self.last_stats_check = 0.0
        # Когда в следующий раз спрашивать /mm. Ноль означает «ещё не
        # назначали» — тогда первый опрос ставится на вход в игру.
        self.stats_next_at = 0.0
        # Разброс, чтобы десяток ботов не опрашивал статистику синхронно.
        self._stats_jitter = 0.0
        self.stats_pending = False
        self.stats_pending_at = 0.0
        self.stats_dialog_id = None

        # Общий ответчик на диалоги: не даёт ответить на один и тот же
        # пункт дважды, если событие поймали несколько обработчиков.
        from logic.menu import MenuAnswerer

        self.menu_answerer = MenuAnswerer(log=lambda m: self.add_log(m))

        # Автосбор урожая («Сборщик»). Ведёт диалоги работы и закрывает
        # мини-игру сбора. Свой ответчик не нужен — берёт menu_answerer.
        from logic.harvester import Harvester, autostart_enabled

        self.harvester = Harvester()
        # Автосбор включается только явно: BR_AUTOHARVEST=1. По умолчанию
        # молчит, иначе бот на шахте переключился бы на сборщика урожая,
        # увидев любое меню работ.
        if autostart_enabled():
            self.harvester.start(self)

        self.logs = deque(maxlen=MAX_LOG_HISTORY)
        self.status = "ЗАБАНЕН" if self.is_banned else "Остановлен"
        self.connected_time = None
        self.thread = None
        self.active_task = None

        # Спавн: watchdog выводит бота из зоны авторизации на вокзал
        self.spawn_watchdog = None
        # Идёт отложенный ответ на меню вокзала. Сервер присылает меню
        # несколько раз подряд, и без этого флага на каждое заводился бы
        # свой поток с ответом.
        self.spawn_answer_busy = False
        self.spawned_at = None
        self.spawn_point = None
        self.spawn_report = None
        # Проверка общего мира выполняется один раз за вход; при новом
        # подключении снимается, иначе после реконнекта бот остался бы
        # без проверки стрима.
        self._real_world_checked = False

        self.reconnect_thread = None
        self.reconnect_cancel_event = threading.Event()
        self.scheduled_reconnect_delay = None
        # До какого момента бот намеренно не в игре (почасовой цикл payday).
        # Нужен, чтобы монитор 24/7 не поднимал его раньше времени.
        self.paused_until = 0.0

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
        # Позиция на вокзале ещё не значит, что бот в общем мире: если
        # ответный Spawn не ушёл, стрим остаётся пустым. Проверяем один
        # раз за вход, в фоне — вход не должен ждать сорок секунд.
        self.start_real_world_check()
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

    def start_real_world_check(self):
        """Один раз за вход довести бота до общего мира.

        Проверка идёт в своём потоке: она ждёт игроков в стриме до сорока
        секунд, и держать на этом вход нельзя. Флаг снимается при новом
        подключении, поэтому после переподключения проверка повторится.
        """
        if getattr(self, "_real_world_checked", False):
            return False
        # Движок качки сам ведёт бота в мир. Второй ensure в фоне начал бы
        # двигать ту же сессию своим warp_to_station — два потока на одного
        # бота дают откат позиции и пустой стрим.
        if getattr(self, "_lvl_engine_active", False):
            return False
        self._real_world_checked = True
        try:
            from logic.real_world import ensure_async
        except Exception as exc:
            self.add_log(f"[МИР] проверка недоступна: {exc}")
            return False
        ensure_async(self, log=self.add_log)
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
        """Ручной выбор вокзала: индекс или дефолтный для этого бота.

        Только через меню. RPC-хендшейк (request_class + spawn) больше не
        шлём: сервер отвечает на него киком античита кодом 25. Если меню
        не пришло — возвращаем отчёт с позицией, ничего не ломая.
        """
        from logic.spawn import (
            menu_choice, select_spawn, spawn_index_for, wait_spawn_menu,
        )

        idx = spawn_index_for(self.bot_id) if index is None else int(index)
        items = wait_spawn_menu(self, 30.0)
        if not items:
            report = {
                "status": "no_menu",
                "position": self.api.getbotposition() if self.api else None,
                "reason": "меню вокзалов не пришло за 30с",
            }
            self.add_log("[SPAWN] меню не пришло, хендшейк не трогаю")
            self.spawn_report = report
            return report

        select_spawn(self, menu_choice(items, self))
        report = {
            "status": "menu_sent",
            "position": self.api.getbotposition() if self.api else None,
        }
        self.spawn_report = report
        return report

    def setup_session(self):
        self.stopping = False
        self._real_world_checked = False
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
            # Внешность — своя у каждого аккаунта.
            #
            # Раньше ВСЕ боты регистрировались с gender=0 и skin=78. Два
            # десятка одинаковых персонажей, зашедших с одного адреса, —
            # это подпись даже без анализа поведения: админу достаточно
            # посмотреть на список игроков.
            #
            # Значение выводится из ника, поэтому при переподключении и
            # повторном заходе персонаж остаётся тем же — как у живого.
            appearance = _appearance_for(self.nickname)
            reg_config = RegistrationConfig(
                enabled=True,
                auto_login=True,
                password=self.password,
                gender=appearance["gender"],
                skin=appearance["skin"],
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

    def _try_auto_proxy(self) -> bool:
        """Прямой вход не проходит — подобрать прокси из пула.

        Зачем: на части машин до игровых серверов не доходит трафик с
        прямого IP. TCP-соединение при этом устанавливается за десятки
        миллисекунд, поэтому снаружи всё выглядит исправным, а на деле
        запросы уходят в пустоту: Send-Q висит, ответа нет. Бот в таком
        состоянии бесконечно пишет «Сервер не ответил на подключение».
        Через прокси тот же сервер отвечает сразу.

        Возвращает True, если прокси назначен и есть смысл переподключиться.
        """
        self._auto_proxy_tried = True
        mgr = getattr(self, "manager", None)
        pool = getattr(mgr, "proxy_pool", None) if mgr else None
        if pool is None:
            return False
        try:
            urls = pool.usable_proxies()
        except Exception as exc:
            self.add_log(f"[ПРОКСИ] не смог получить список рабочих: {exc}")
            return False
        if not urls:
            self.add_log(
                "[ПРОКСИ] прямой вход не проходит, а рабочих прокси в пуле нет. "
                "Проверь «Прокси и VPN» — без них на этой машине не зайти."
            )
            return False

        # Берём наименее загруженный узел: иначе все боты сядут на один
        # адрес, а это ровно та причина, по которой сервер банит пачками.
        counts: dict[str, int] = {}
        try:
            with mgr._lock:
                for other in mgr.bots.values():
                    if getattr(other, "proxy", ""):
                        counts[other.proxy] = counts.get(other.proxy, 0) + 1
        except Exception:
            pass
        url = min(urls, key=lambda u: counts.get(u, 0))

        self.proxy = url
        self.add_log(
            f"[ПРОКСИ] прямой вход не проходит ({self.connect_failures} попыток). "
            f"Переключаюсь на {url} — ботов на нём: {counts.get(url, 0)}."
        )
        try:
            if mgr is not None:
                mgr.save_accounts()
        except Exception:
            pass
        return True

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
        if self.manager and hasattr(self.manager, "_ensure_proxy_watchdog"):
            self.manager._ensure_proxy_watchdog()

        return True

    # Через сколько после входа первый опрос /mm и как часто дальше.
    STATS_FIRST_DELAY = (120.0, 300.0)
    STATS_INTERVAL = (1700.0, 1900.0)

    def stats_interval(self) -> float:
        """Период опроса статистики с разбросом."""
        import random as _random

        return _random.uniform(*self.STATS_INTERVAL)

    def arm_stats(self) -> None:
        """Назначить первый опрос /mm на потом, а не на сейчас.

        Зовётся при входе в игру: до выбора вокзала бот не должен ничего
        писать в чат.
        """
        import random as _random

        self.stats_next_at = time.time() + _random.uniform(*self.STATS_FIRST_DELAY)

    def _stats_tick(self, now: float) -> None:
        """Один тик проверки статистики (вызывается общим шедулером менеджера).

        Счёт идёт от `stats_next_at`, а не от «прошло ли 1800 секунд с
        нулевого времени». Раньше `last_stats_check` был 0.0, а сравнивался
        с epoch — условие было истинно всегда, и бот отправлял /mm на первом
        же тике после авторизации, ещё до выбора вокзала. Свежий игрок,
        который первым делом лезет в настройки безопасности, — это и есть
        тот признак, за который античит кикает кодом 25.
        """
        if self.stopping or self.is_banned:
            return
        if not (self.session and self.session.connected):
            return
        if "В игре" not in self.status:
            return
        # Зависший запрос: отправили /mm, а меню не разобралось. Держать
        # флаг поднятым нельзя — пока он поднят, любой диалог со словом
        # «уровень» считается экраном статистики.
        if self.stats_pending and self.stats_pending_at:
            if now - self.stats_pending_at > 120.0:
                self.stats_pending = False
                self.stats_pending_at = 0.0
                self.add_log(
                    "[СТАТИСТИКА] /mm не разобрался за 120 с — сбрасываю запрос"
                )

        if not self.stats_next_at:
            # Первый тик после входа: назначаем окно, ничего не отправляем.
            self.arm_stats()
            return
        if now < self.stats_next_at:
            return
        self.stats_pending = True
        self.stats_pending_at = now
        self.send_chat("/mm")
        self.last_stats_check = now
        self.stats_next_at = now + self.stats_interval()

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

            # Прямой вход стабильно не проходит — возможно, с этого IP
            # сервер недоступен вовсе. Один раз пробуем прокси.
            if (
                self.connect_failures >= DIRECT_FALLBACK_AFTER
                and not self.proxy
                and not self._auto_proxy_tried
            ):
                if self._try_auto_proxy():
                    self.connect_failures = 0
                    self.schedule_reconnect(delay=3.0, force=True)
                    return

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

    def pause_until(self, when: float, reason: str = "") -> float:
        """Выйти из игры и вернуться в назначенное время.

        Раньше «перерыв» делался через stop(): он ставит stopping=True и
        отменяет переподключение, а монитор 24/7 такие аккаунты не поднимает.
        То есть бот уходил на отдых и не возвращался никогда.

        Здесь наоборот: ставим задержку, гасим задачу, отключаемся — и
        штатный обработчик disconnect сам планирует вход через эту задержку.
        """
        delay = max(PAUSE_MIN, float(when) - time.time())
        self.scheduled_reconnect_delay = delay
        self.paused_until = time.time() + delay
        self.add_log(
            f"[ПАУЗА] {reason or 'перерыв'}: выход, вход через "
            f"{delay / 60:.0f} мин"
        )
        if self.active_task:
            try:
                self.active_task.stop()
            except Exception:
                pass
            self.active_task = None
        try:
            if self.api:
                self.api.disconnect()
        except Exception:
            pass
        # Если событие disconnect почему-то не придёт, таймер всё равно
        # поставим сами. Повторный вызов безопасен: schedule_reconnect
        # игнорирует второй вызов, пока висит первый.
        self.schedule_reconnect(delay=delay)
        return delay

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
            # Новый движок: подтверждение мира, вокзал, минута после
            # спавна, бессмертие, точка из lvl_targets.json, /bg.
            self.active_task = LevelRunnerTask(self)
            self.active_task.start()
            return "Качка LVL запущена (движок: мир → точка → /bg)"
        elif task_type in ("lvl_old", "lvl_classic", "lvl2"):
            self.active_task = LevelGrinderTask(self)
            self.active_task.start()
            return "Качка LVL запущена (старый цикл)"
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
            return ("Неизвестный тип задачи. Доступно: 'lvl', 'lvl_old', 'quest', "
                "'mine', 'factory', 'bus', 'stop'")

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
        # Автозамена банов: сериализуем создания и держим лимит в час.
        self._replace_lock = threading.Lock()
        self._replace_recent: list[float] = []

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

        # Путь к реестру — поле экземпляра, а не глобальная константа.
        # Иначе тест, подменивший bot.ACCOUNTS_FILE на время, оставлял после
        # себя живые фоновые нити, которые дописывали файл уже после снятия
        # подмены — и стирали настоящий accounts.json двумя тестовыми ботами.
        # Переменная читается здесь, а не при импорте модуля: иначе тест не
        # может подставить свой файл, и менеджер успевает загрузить боевой
        # реестр до подмены.
        self.accounts_file = os.environ.get("BR_ACCOUNTS_FILE") or ACCOUNTS_FILE
        # Предохранитель: под unittest настоящий реестр не трогаем никогда,
        # даже если тест забыл подставить свой путь.
        if os.environ.get("BR_TEST_MODE") and self.accounts_file == ACCOUNTS_FILE:
            self.accounts_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "accounts.test.json",
            )

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

        # Настройки: базовый пароль для новых аккаунтов и задача по
        # умолчанию. Нужны до load_accounts и до любой регистрации.
        try:
            from logic.settings import get_settings

            self.settings = get_settings(log=lambda msg: print(msg, flush=True))
        except Exception as exc:
            self.settings = None
            print(f"[-] Настройки не поднялись: {exc}")

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

    def mark_bot_banned(self, bot: BotInstance, reason: str = "", raw: str = ""):
        """Пометить аккаунт забаненным, разобрав кто и за что.

        `raw` — исходная строка от сервера (чат или текст диалога). Из неё
        достаётся ник администратора, причина и срок; в Telegram уходит уже
        разобранное, а не вся строка целиком.

        Сервер сообщает об одном и том же бане дважды — строкой в чат и
        диалогом при следующем входе. Без защиты это давало два оповещения
        подряд и два «новых аккаунта взамен». Повторный вызов в пределах
        BAN_ALERT_DEDUPE секунд игнорируется.
        """
        from logic.baninfo import parse_ban

        info = parse_ban(raw or reason)
        now = time.time()

        # Тот же бан пришёл второй раз (чат + диалог) — не дублируем.
        if bot.is_banned and bot.banned_at and (now - bot.banned_at) < BAN_ALERT_DEDUPE:
            bot.add_log(f"[!] Повторное сообщение о бане проигнорировано: {info.summary}")
            return

        bot.is_banned = True
        bot.ban_admin = info.admin
        bot.ban_reason = info.reason or reason
        bot.ban_kind = info.kind
        bot.ban_duration = info.duration
        bot.banned_at = now
        bot.cancel_reconnect()
        bot.status = "ЗАБАНЕН"
        bot.add_log(f"[!] АККАУНТ ЗАБАНЕН! {info.summary} | сырое: {reason}")
        print(f"\n[!] ВНИМАНИЕ: Бот #{bot.bot_id} [{bot.nickname}] получил БАН! "
              f"({info.summary})")
        bot.stop(banned=True)
        self.save_accounts()

        # 1. Отправляем в Telegram и ИИ-анализ если подключен Telegram контроллер
        if hasattr(self, "tg_controller") and self.tg_controller:
            if info.kind == "manual":
                title = "🚨 <b>АККАУНТ ЗАБАНЕН АДМИНИСТРАТОРОМ</b>"
            elif info.kind == "auto":
                title = "🚨 <b>АККАУНТ ЗАБАНЕН СЕРВЕРОМ</b>"
            else:
                title = "🚨 <b>АККАУНТ ЗАБАНЕН</b>"

            lines = [
                title,
                f"• Бот: #{bot.bot_id} <b>{bot.nickname}</b>",
                f"• Сервер: <b>{bot.server_name}</b>",
            ]
            if info.admin:
                role = info.role.capitalize() if info.role else "Администратор"
                lines.append(f"• Забанил: <b>{info.admin}</b> ({role})")
            else:
                lines.append("• Забанил: <i>автоматически, без администратора</i>")
            if info.reason:
                lines.append(f"• Причина: <code>{info.reason}</code>")
                if info.reason_human and info.reason_human.lower() not in info.reason.lower():
                    lines.append(f"• Тип: <i>{info.reason_human}</i>")
            else:
                lines.append("• Причина: <i>сервер не указал</i>")
            if info.duration:
                lines.append(f"• Срок: <b>{info.duration}</b>")
            if info.issuer:
                lines.append(f"• Выписал: <i>{info.issuer}</i>")
            if info.target and info.target.lower() != str(bot.nickname).lower():
                lines.append(f"• В сообщении игрок: <code>{info.target}</code>")
            alert_text = "\n".join(lines) + "\n"
            # Отправляем в ИИ для анализа причины и рекомендаций
            if hasattr(self.tg_controller, "ai") and self.tg_controller.ai.config.get("api_key"):
                def _ai_ban_inspect():
                    who = f"администратор {info.admin}" if info.admin else "сервер автоматически"
                    ai_res = self.tg_controller.ai.execute_natural_command(
                        f"Бот {bot.nickname} на сервере {bot.server_name} получил бан. "
                        f"Забанил: {who}. Причина: '{info.reason or reason}'. "
                        f"Срок: {info.duration or 'не указан'}. Проанализируй причину."
                    )
                    self.tg_controller.broadcast(f"{alert_text}\n🤖 <b>Анализ ИИ:</b>\n{ai_res}")
                threading.Thread(target=_ai_ban_inspect, daemon=True).start()
            else:
                self.tg_controller.broadcast(alert_text)

        # 2. Автозамена: новый аккаунт взамен забаненного.
        #
        # Раньше замена срабатывала только если в тексте бана встречалось
        # слово из узкого списка: «бот», «чит», «кликер», «постороннее ПО».
        # Сервер в большинстве случаев пишет другое — «автобан сервера
        # (без админа и причины)» или «Ваш игровой аккаунт заблокирован,
        # вход невозможен» — и ни одно слово не совпадало, замена не
        # создавалась. Из 105 банов в логе замену получили 16.
        #
        # Замену не делаем только в одном случае: IP-бан (kick 999,
        # CONNECTION_BANNED). Он на адрес, а не на аккаунт — новый ник с
        # того же IP забанят так же, и регистрация сгорит впустую.
        ip_banned = "connection_banned" in (reason or "").lower()
        if ip_banned:
            # Бан по IP: сервер не пускает никого с этого адреса. Помечаем
            # прокси непригодным навсегда и переносим на него метку всем,
            # кто с него сидел. Иначе следующий бот возьмёт тот же адрес
            # из пула и словит тот же бан — а мы будем думать, что дело
            # в аккаунтах.
            self.handle_ip_ban(bot, reason=reason or "connection banned")
        if not ip_banned and not getattr(bot, "_replaced", False):
            bot._replaced = True

            def _create_replacement():
                # Пароль берём базовый из настроек: он одинаков для всех
                # новых аккаунтов, и в реестре не плодятся разные пароли.
                # Если настройки пусты — пароль забаненного.
                settings = getattr(self, "settings", None)
                pwd = settings.password_or(bot.password) if settings else bot.password
                task = bot.default_task_name
                if not task and settings is not None:
                    task = settings.default_task

                # Сериализуем замены: одновременно создаётся ровно одна,
                # с паузой между попытками и лимитом в час. Без этого
                # волна банов порождала полсотни регистраций разом, и
                # сервер отвечал блокировкой за частые входы.
                with self._replace_lock:
                    now_ts = time.time()
                    self._replace_recent = [
                        t for t in self._replace_recent if now_ts - t < 3600.0
                    ]
                    if len(self._replace_recent) >= REPLACE_PER_HOUR:
                        print(
                            f"[-] Автозамена: лимит {REPLACE_PER_HOUR} в час "
                            f"исчерпан, #{bot.bot_id} [{bot.nickname}] без замены"
                        )
                        bot.add_log(
                            f"[!] Замена не создана: лимит {REPLACE_PER_HOUR} "
                            f"аккаунтов в час исчерпан."
                        )
                        return

                    if self._replace_recent:
                        wait = self._replace_recent[-1] + REPLACE_INTERVAL - time.time()
                        if wait > 0:
                            time.sleep(min(wait, REPLACE_INTERVAL))
                    else:
                        # Первая замена после паузы — не мгновенно, чтобы не
                        # совпасть с самим баном.
                        time.sleep(random.uniform(*REPLACE_FIRST_DELAY))

                    print(f"[*] Создаем новый аккаунт взамен забаненного бота на сервере {bot.server_name}...")
                    self._replace_recent.append(time.time())
                    new_bot = self.add_bot(
                        target_server=bot.server_name,
                        nickname=None,  # None -> сгенерировать реалистичный ник
                        password=pwd,
                        auto_regen_nick=True,
                        proxy=bot.proxy,
                        default_task=task,
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
                                # Намеренная пауза (почасовой цикл payday) —
                                # не поднимаем раньше срока, иначе бот вернётся
                                # в игру вне своего окна и словит payday мимо.
                                if b.paused_until and time.time() < b.paused_until:
                                    continue
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
                # Новая сессия — старое меню вокзалов недействительно.
                # Сбрасываем только своё: раньше сброс был глобальным и
                # стирал меню всем остальным ботам в процессе.
                from logic.spawn import reset_menu

                reset_menu(b)
                # Вход разрешён — очередь сброшена
                b.leave_queue()
                b.status = "В игре (авторизован)"
                b.connected_time = time.time()
                # Новый вход — новый отсчёт до /mm. Иначе после реконнекта
                # бот сразу лезет в статистику, едва авторизовавшись.
                b.stats_next_at = 0.0
                b.stats_pending = False
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
                answer_spawn_later,
                in_auth_zone,
                is_spawn_menu,
                menu_choice,
                remember_spawn_menu,
            )

            payload = data if isinstance(data, dict) else {}

            if is_spawn_menu(payload):
                items = remember_spawn_menu(payload, b)
                choice = menu_choice(items, b)
                b.add_log(f"[SPAWN] меню вокзалов {items}, выбираем {choice}")
                # Отвечаем не в этом тике, а через человеческую паузу и в
                # отдельном потоке: sleep здесь застопорил бы разбор пакетов,
                # а мгновенный ответ выдаёт бота — античит кикает кодом 25.
                answer_spawn_later(b, choice)
                return

            # {'s': N} — либо приглашение, либо подтверждение выбора.
            # Приглашение {"s": 1} без списка сервер не обслуживает,
            # вокзал выбирается RPC-хендшейком.
            if not payload.get("s"):
                return

            pos = b.api.getbotposition() if b.api else None
            if available_spawns(b) and in_auth_zone(pos):
                if b.spawn_watchdog is None or not b.spawn_watchdog.running:
                    b.begin_spawn_watchdog()

        @emitter.on("onShowChat")
        def _on_chat(bot_obj, message, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                # Метка живого чата: по ней вход в мир подтверждается, когда
                # стрим рядом пуст, а люди на сервере пишут.
                b.last_chat_at = time.time()
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

                # 0.5. Блокировка по IP. Проверяем ПЕРВОЙ: античит пишет
                # «Ваш IP-адрес был заблокирован», и это не бан аккаунта.
                # Аккаунт цел, а адрес — нет. Если не отличить, аккаунт
                # уйдёт в мусор, а следующий бот возьмёт тот же IP из пула
                # и получит то же самое.
                if is_ip_block(clean_msg):
                    self.handle_ip_ban(b, reason=f"Античит: {clean_msg[:120]}")
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
                is_freq_ban = is_freq_ban_text(clean_msg)

                if is_freq_ban:
                    b.add_log(f"[РЕКОННЕКТ] Бан за частые входы/лимит времени (20 мин): '{clean_msg}'. Вход через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0
                    return

                if is_admin_ban:
                    self.mark_bot_banned(b, reason=f"Чат: {clean_msg}", raw=clean_msg)
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
                b.add_log(
                    f"[ДИАЛОГ #{d_id} стиль={style}] {title} | Кнопки: '{b1}' / '{b2}'"
                )
                if os.environ.get("BR_TRACE_JSON"):
                    body = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(info))
                    b.add_log(f"[ДИАЛОГ-ТЕЛО] {body[:400]!r}")
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

                # 1. Меню /mm: первый экран — список пунктов, второй — данные.
                #
                # Раньше меню искали по строкам «главное меню» и «меню
                # персонажа», а сервер пишет «Меню игрока». Меню не
                # распознавалось, зато следующая ветка ловила слово
                # «Статистика» из СПИСКА ПУНКТОВ и парсила меню как экран
                # статистики: цифр не находила и просто закрывала диалог.
                # Итог — /mm уходил в пустоту, уровень в реестре оставался «-».
                from logic.menu import decide_stats_flow, dialog_signature, parse_stats

                # Отпечаток текущего экрана. Все диалоги BR приходят с id 0,
                # поэтому «тот же диалог» отличаем по содержимому.
                sig = dialog_signature(style, title, info)

                decision = decide_stats_flow(
                    style, title, info, stats_pending=bool(b.stats_pending)
                )

                # Экран 1: меню игрока -> выбрать «Статистика».
                # Экран 2: данные -> разобрать и закрыть.
                if decision is not None and decision.action == "pick":
                    def _pick_stat(dialog_id=int(d_id), item=int(decision.list_item),
                                   note=str(decision.note), signature=sig):
                        # Пауза как у человека: он читает список, а не
                        # щёлкает в тот же кадр, что меню появилось.
                        time.sleep(random.uniform(0.35, 0.9))
                        b.menu_answerer.answer(
                            b.session, dialog_id, list_item=item, response=1,
                            note=note, signature=signature,
                        )
                    threading.Thread(target=_pick_stat, daemon=True).start()
                    b.add_log(f"[МЕНЮ] /mm: {decision.note} (пункт {decision.list_item})")

                elif decision is not None and decision.action == "close":
                    def _close_menu(dialog_id=int(d_id), note=str(decision.note),
                                    signature=sig):
                        time.sleep(0.4)
                        b.menu_answerer.answer(
                            b.session, dialog_id, list_item=0, response=0,
                            note=note, signature=signature,
                        )
                    threading.Thread(target=_close_menu, daemon=True).start()
                    b.add_log(f"[МЕНЮ] /mm: {decision.note}")

                # --- экран 2: сами данные (вложенный диалог) ---
                if decision is not None and decision.screen == "stats":
                    # Ищем уровень и exp: например "Уровень: 2", "Игровой уровень: 2", "Очки опыта: 2/8", "EXP: 2/8", "Опыт: 2 из 8"
                    # Также форматы: "2 lvl", "2/8 exp"
                    stats = parse_stats(info)
                    found_lvl = stats["level"]
                    if stats["money"]:
                        b.money = int(stats["money"])

                    if found_lvl:
                        # Уровень и опыт пишутся через общий слой: строка
                        # «3 lvl · 12/40», числа и отметка времени. Панель
                        # и Telegram читают одно и то же, расхождений нет.
                        from logic.stats import apply as apply_stats

                        apply_stats(b, stats)
                        b.last_stats_check = time.time()
                        b.stats_next_at = b.last_stats_check + b.stats_interval()
                        b.stats_pending = False
                        b.add_log(
                            f"[СТАТИСТИКА] Уровень: {b.level_str}, "
                            f"баланс: {b.get_balance_str()}"
                        )
                        self.save_accounts()
                        b.check_target_level_achieved()
                    else:
                        b.add_log(
                            "[СТАТИСТИКА] Экран статистики пришёл, но уровень "
                            f"в нём не разобрался: {stats['raw'][:90]!r}"
                        )

                    # Закрываем диалог статистики, чтобы не висел поверх игры.
                    # Именно этот второй диалог и есть «вложенное меню»:
                    # сначала список пунктов, потом данные по выбранному.
                    def _close_stats(dialog_id=int(d_id), signature=sig):
                        time.sleep(0.35)
                        b.menu_answerer.answer(
                            b.session, dialog_id, list_item=0, response=0,
                            note="статистика: закрываю", signature=signature,
                        )
                    threading.Thread(target=_close_stats, daemon=True).start()

                # Блокировка по IP приходит и диалогом от античита.
                # Проверяем раньше общего списка: иначе «IP-адрес
                # заблокирован» попадёт под «заблокирован» и аккаунт
                # уйдёт в бан, хотя виноват адрес.
                if is_ip_block(f"{title} {info}"):
                    clean_ip_info = re.sub(r'\{[0-9a-fA-F]{6}\}', '', f"{title} {info}")
                    b.add_log(f"[IP] Античит заблокировал адрес: {clean_ip_info[:120]}")
                    self.handle_ip_ban(b, reason=f"Античит: {clean_ip_info[:140]}")
                    return

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
                # Кик античита — отдельный случай: это не бан аккаунта на
                # сервере, а запрет заходить с этого клиента. Перезаход тут
                # бесполезен и только усугубляет, поэтому помечаем аккаунт
                # и останавливаем его.
                kick_code = anticheat_kick(title, clean_info)
                if kick_code is not None:
                    # Кик кодом 25 — не бан аккаунта, а отказ клиенту.
                    #
                    # Раньше здесь стоял mark_bot_banned, и аккаунт уходил
                    # в перманентный бан навсегда. Но причина кика была
                    # наша собственная: RPC-хендшейк спавна при открытом
                    # меню вокзалов. Так «отлетало» большинство аккаунтов,
                    # хотя на сервере они целы.
                    #
                    # Хендшейк выключен (ALLOW_RPC_SPAWN_HANDSHAKE), так
                    # что кика быть не должно. Если он всё же пришёл —
                    # это повод отойти на паузу, а не жечь аккаунт.
                    b.add_log(
                        f"[АНТИЧИТ] кик кодом {kick_code}: {clean_info[:80]}"
                        " — пауза 25 минут, аккаунт НЕ баню"
                    )
                    b.scheduled_reconnect_delay = 25 * 60.0
                    return

                # Исключаем временный бан за частые входы из перманентного бана
                is_freq_ban = is_freq_ban_text(dialog_text)
                if is_freq_ban:
                    b.add_log(f"[РЕКОННЕКТ] Бан за частые входы в диалоге #{d_id}. Вход через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0
                elif any(trigger in dialog_text for trigger in ban_triggers) or any(
                    pattern.search(dialog_text) for pattern in ban_patterns
                ):
                    self.mark_bot_banned(
                        b,
                        reason=f"Диалог #{d_id}: {title} | {clean_info[:120]}",
                        raw=f"{title} {info}",
                    )

                # Много заходов с одного IP в диалоге
                if "много заходов" in dialog_text or ("ip" in dialog_text and ("слишком много" in dialog_text or "подозрител" in dialog_text or "повторите попытку позже" in dialog_text)):
                    b.add_log(f"[РЕКОННЕКТ] Диалог #{d_id}: много заходов с IP. Ожидание 5 минут...")
                    b.scheduled_reconnect_delay = 5 * 60.0

                # Автосбор урожая: меню устройства на работу и подтверждение
                # сбора. Стоит последним намеренно — выше проверяются бан,
                # кик античита и лимиты, и они должны срабатывать первыми.
                try:
                    from logic.harvester import handle_harvest_dialog

                    if handle_harvest_dialog(b, d_id, style, title, info, b1, b2):
                        b.add_log(f"[АВТОСБОР] диалог #{d_id} обработан")
                except Exception as exc:
                    b.add_log(f"[АВТОСБОР] ошибка обработки диалога: {exc}")

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
            if os.environ.get("BR_TRACE_JSON"):
                try:
                    b.add_log(f"[ТРЕЙС] iid={interface_id} {str(json_data)[:220]}")
                except Exception:
                    pass
            # Помощники сюжетки видят всё: диалоги НПС, ларёк, звонки, палитру.
            try:
                handled = b.quest_helpers.on_json(interface_id, json_data)
            except Exception as exc:
                b.add_log(f"[ПОМОЩНИК] сбой на iid={interface_id}: {exc!r}")
                handled = None
            # Капча приходит JSON-интерфейсом 26, URL картинки — в ключе "u".
            # Ответ: {"t": 0, "s": "<текст>"} в тот же интерфейс.
            # Почему именно так и что проверено как нерабочее — logic/captcha.py.
            try:
                from logic.captcha import handle_captcha
                if handle_captcha(b, interface_id, json_data):
                    handled = True
            except Exception as exc:
                b.add_log(f"[КАПЧА] сбой на iid={interface_id}: {exc!r}")
            # Мини-игры — JSON интерфейс 31. Попадание клиент считает локально
            # и просто сообщает серверу {"s":1}; автопрохождение = послать это
            # сразу. Разбор режимов — logic/minigames.py.
            try:
                from logic.minigames import handle_minigame
                if handle_minigame(b, interface_id, json_data):
                    handled = True
            except Exception as exc:
                b.add_log(f"[МИНИ-ИГРА] сбой на iid={interface_id}: {exc!r}")
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

        @emitter.on("OnDisableCheckpoint")
        def _on_checkpoint_cleared(bot_obj, *args, **kwargs):
            # Маркер снят — цель отработана. Движок квестов не должен
            # продолжать ехать к точке, которой уже нет.
            b = self._find_bot_by_session_or_api(bot_obj)
            if not b:
                return
            quest = getattr(b, "slava_quest", None)
            if quest is not None:
                try:
                    quest.on_checkpoint_cleared()
                except Exception as exc:
                    b.add_log(f"[СЛАВА] сброс маркера упал: {exc!r}")

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
        if not os.path.exists(self.accounts_file):
            return

        try:
            with open(self.accounts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            loaded_count = 0
            skipped_dupes = 0
            skipped_bad = 0
            seen_nicks = set()
            # Все ники из реестра — чтобы генератор не выдал занятый.
            # Без этого после перезапуска процесса бот мог получить ник,
            # который уже есть у другого аккаунта.
            known = {
                str(item.get("nickname")).strip()
                for item in data
                if isinstance(item, dict) and item.get("nickname")
            }
            self._known_nicknames = known
            for item in data:
                # Разбор каждой записи отдельно и без предположений о типах.
                #
                # Одна запись с "nickname": null роняла загрузку ВСЕГО файла:
                # .get("nickname", "") возвращает None, если ключ есть и равен
                # null, а не подставляет "", и .strip() падал. Исключение
                # ловилось снаружи цикла, поэтому всё, что шло после битой
                # записи, просто не загружалось.
                if not isinstance(item, dict):
                    skipped_bad += 1
                    continue

                nick = str(item.get("nickname") or "").strip()
                pwd = str(item.get("password") or "").strip()
                srv_str = str(item.get("server") or "").strip()
                task = item.get("task") or "lvl"
                is_banned = bool(item.get("is_banned", False))
                ban_admin = str(item.get("ban_admin") or "")
                ban_reason = str(item.get("ban_reason") or "")
                ban_kind = str(item.get("ban_kind") or "")
                ban_duration = str(item.get("ban_duration") or "")
                try:
                    banned_at = float(item.get("banned_at") or 0.0)
                except (TypeError, ValueError):
                    banned_at = 0.0
                proxy = item.get("proxy") or ""
                proxy_node = str(item.get("proxy_node") or "")
                saved_lvl = item.get("lvl") or "-"
                try:
                    saved_money = int(item.get("money") or 0)
                except (TypeError, ValueError):
                    saved_money = 0
                saved_target_lvl = item.get("target_lvl")
                if saved_target_lvl is not None:
                    try:
                        saved_target_lvl = int(saved_target_lvl)
                    except Exception:
                        saved_target_lvl = None

                if not nick or not pwd or not srv_str:
                    skipped_bad += 1
                    continue

                # Один ник — один бот, даже если в файле он записан
                # несколько раз. Иначе все копии лезут в один аккаунт,
                # выбивают друг друга киком «уже в сети» и мешают спавну.
                nick_key = nick.lower()
                if nick_key in seen_nicks:
                    skipped_dupes += 1
                    continue
                seen_nicks.add(nick_key)

                try:
                    srv = br_servers.find_server(srv_str)
                except Exception:
                    srv = None

                if not srv:
                    # Список серверов мог не прийти (сеть, API лежит). Раньше
                    # такой аккаунт молча отбрасывался, и при лежащем API
                    # пропадала вся ферма. Теперь берём адрес, сохранённый в
                    # самом аккаунте: он уже прописан и рабочий.
                    saved_ip = item.get("ip") or item.get("host")
                    saved_port = item.get("port") or 5125
                    if not saved_ip:
                        skipped_bad += 1
                        continue
                    ip = saved_ip
                    port = int(saved_port)
                    clean_name = (item.get("server_name") or srv_str or "").upper()
                    if not clean_name:
                        skipped_bad += 1
                        continue
                else:
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
                        proxy_node=proxy_node,
                    )
                    b.ban_admin = ban_admin
                    b.ban_reason = ban_reason
                    b.ban_kind = ban_kind
                    b.ban_duration = ban_duration
                    b.banned_at = banned_at
                    b.level_str = saved_lvl
                    b.money = saved_money
                    b.target_lvl = saved_target_lvl
                    self.bots[bot_id] = b
                    if self.active_bot_id is None:
                        self.active_bot_id = bot_id
                loaded_count += 1

            if loaded_count > 0:
                print(f"[+] Загружено {loaded_count} сохраненных аккаунтов из {os.path.basename(self.accounts_file)}")
            if skipped_dupes:
                print(
                    f"[!] Пропущено {skipped_dupes} повторных записей одного и того же "
                    f"ника — в файле есть дубли"
                )
            if skipped_bad:
                print(f"[!] Пропущено {skipped_bad} битых записей в accounts.json")
        except Exception as e:
            print(f"[!] Ошибка загрузки {self.accounts_file}: {e}")
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
                    "ban_admin": getattr(b, "ban_admin", "") or "",
                    "ban_reason": getattr(b, "ban_reason", "") or "",
                    "ban_kind": getattr(b, "ban_kind", "") or "",
                    "ban_duration": getattr(b, "ban_duration", "") or "",
                    "banned_at": getattr(b, "banned_at", 0.0) or 0.0,
                    "proxy": b.proxy,
                    # Ключ узла под мостом. Порт моста меняется после
                    # перезапуска, и одного порта мало: аккаунт начинал
                    # смотреть в чужой или мёртвый туннель.
                    "proxy_node": getattr(b, "proxy_node", "") or "",
                    "lvl": b.level_str,
                    "money": b.money,
                    "target_lvl": b.target_lvl,
                })

        self._write_accounts(data)

    def _write_accounts(self, data: list) -> None:
        """Записать accounts.json атомарно и оставить одну резервную копию.

        Раньше файл писался поверх сразу, без копии. Один запуск с пустым
        списком ботов (например, скрипт, который создал менеджер и упал на
        загрузке) стирал весь список аккаунтов — восстановить было нечем.
        Теперь запись идёт через временный файл и подмену, а предыдущая
        версия остаётся в accounts.json.bak.
        """
        path = getattr(self, "accounts_file", ACCOUNTS_FILE)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(path):
                try:
                    shutil.copyfile(path, path + ".bak")
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception as e:
            print(f"[!] Ошибка сохранения {path}: {e}")
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

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
        # Ник может прийти None — так его передаёт автозамена забаненного
        # аккаунта, рассчитывая на генерацию. Раньше генерации не было, и в
        # accounts.json уезжало "nickname": null: запись есть, а бота за ней
        # нет, и загрузить её потом нельзя. Генерируем здесь же.
        if nickname is None or not str(nickname).strip():
            nickname = self.generate_random_nickname()
            print(f"[*] Ник не задан — сгенерирован: {nickname}")
        else:
            nickname = str(nickname).strip()

        if not password or not str(password).strip():
            # Пароль по умолчанию из настроек: без него аккаунт не создать,
            # а при автозамене и массовой регистрации спрашивать его каждый
            # раз незачем.
            settings = getattr(self, "settings", None)
            password = settings.password_or() if settings is not None else ""
            if not password:
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
            # Один ник — одна запись. Раньше проверки не было, и повторное
            # добавление плодило дубли: в accounts.json копились по восемь
            # записей одного ника, а потом все они по очереди лезли в один
            # и тот же аккаунт и выбивали друг друга киком «уже в сети».
            for existing in self.bots.values():
                if str(existing.nickname).lower() == str(nickname).lower():
                    print(
                        f"[!] Бот с ником {nickname} уже есть (ID "
                        f"{existing.bot_id}) — второй раз не добавляю."
                    )
                    return existing

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
        """Ник, похожий на настоящего игрока Black Russia.

        Раньше брались случайное имя из names.txt и фамилия из
        surnames.txt. Оба файла — английские (Jacob, Michael, Abramson),
        поэтому выходило «Moira_Amosov»: английское имя с русской фамилией
        в транслите. Живых таких не бывает, и вся сотня аккаунтов
        выглядела как одна сгенерированная пачка.

        Теперь стиль ника собирается по тому же распределению, что и у
        реальных игроков: русские имена с фамилиями, тюркские имена,
        мемные связки вида Sotka_NaCapte, английские пары и выдуманные
        фамилии. Подробности и пулы — в logic/nicks.py.
        """
        if not hasattr(self, "_nick_generator") or self._nick_generator is None:
            from logic.nicks import NickGenerator

            self._nick_generator = NickGenerator()

        if not hasattr(self, "_used_nicknames"):
            self._used_nicknames = set()

        with self._lock:
            current_active = {b.nickname for b in self.bots.values() if b.nickname}

        # Учитываем и уже выданные, и занятые сейчас, и те, что в реестре:
        # после перезапуска процесса «выданные» теряются, а ники заняты.
        avoid = self._used_nicknames | current_active
        known = getattr(self, "_known_nicknames", None)
        if known:
            avoid |= known

        try:
            nick = self._nick_generator.generate(avoid=avoid)
        except Exception:
            # Крайний случай: генератор не смог — отдаём простой ник,
            # чтобы регистрация не встала.
            #
            # Здесь стоял f"Player_{random.randint(1000, 9999)}", и это
            # худший из возможных ников: английское слово, подчёркивание
            # и четыре цифры. Такой аккаунт видно за секунду. Собираем
            # обычную пару «имя_фамилия» напрямую из пулов.
            from logic.nicks import FEMALE_NAMES, MALE_NAMES, SURNAMES

            for _ in range(200):
                nick = f"{random.choice(MALE_NAMES + FEMALE_NAMES)}" \
                       f"_{random.choice(SURNAMES)}"
                if nick not in avoid and len(nick) <= 20:
                    break
            else:
                nick = f"Nick_{random.choice(SURNAMES)}"

        self._used_nicknames.add(nick)
        return nick

    def proxy_for_replacement(self, old_bot) -> str:
        """Какой прокси дать новому аккаунту при автозамене забаненного.

        Режим задаётся в настройках (replace_proxy):
        inherit — тот же, что был у старого (по умолчанию: новый аккаунт
        садится на тот же IP, что и прежний, и не палит «свежий» адрес);
        pool — рабочий узел из пула;
        none — своё IP;
        иначе — имя сохранённого прокси или готовая строка.
        """
        # Берём тот же объект настроек, в который пишет Telegram и панель:
        # отдельный get_settings() дал бы другой экземпляр, и смена режима
        # не доехала бы до замены.
        settings = getattr(self, "settings", None)
        try:
            if settings is None:
                from logic.settings import get_settings
                settings = get_settings()
            mode = settings.replace_proxy
        except Exception:
            mode = "inherit"

        if mode == "inherit":
            return getattr(old_bot, "proxy", "") or ""
        if mode in ("none", "off", "reset", ""):
            return ""

        if mode == "pool":
            pool = getattr(self, "proxy_pool", None)
            if pool is None:
                return getattr(old_bot, "proxy", "") or ""
            try:
                picked = pool.rotate(1)
            except Exception:
                picked = []
            return picked[0] if picked else (getattr(old_bot, "proxy", "") or "")

        # Имя сохранённого прокси из proxies.txt.
        try:
            from tg_controller import load_proxy_list
            saved = dict(load_proxy_list())
        except Exception:
            saved = {}
        if mode in saved and saved[mode]:
            return saved[mode]

        # Готовая строка вида socks5://...
        if "://" in mode or (":" in mode and " " not in mode):
            return mode
        return getattr(old_bot, "proxy", "") or ""

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
        new_proxy = self.proxy_for_replacement(bot)

        bot.stop()

        def _respawn():
            time.sleep(2.0)
            with self._lock:
                if bot.bot_id in self.bots:
                    del self.bots[bot.bot_id]
                self._forget_bot(bot)

            new_b = self.add_bot(srv_name, new_nick, password=pwd, auto_regen_nick=True, default_task=task_name)
            # Прокси для замены берём из настроек: иначе новый аккаунт
            # уходил с прямого IP и палил «свежий» адрес рядом со старым.
            if new_proxy and new_b is not None:
                try:
                    self.set_bot_proxy(new_b.bot_id, new_proxy)
                except Exception as exc:
                    new_b.add_log(f"[ПРОКСИ] замена не применилась: {exc!r}")
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
            settings = getattr(self, "settings", None)
            password = settings.password_or() if settings else ""
            if not password:
                print("[-] Ошибка: Пароль обязателен для создания ботов!")
                return []
            print(f"[*] Пароль не задан — беру базовый из настроек")

        password = password.strip()
        created = []
        # Пустые строки — это «без прокси», а не прокси, который не прошёл
        # проверку. Иначе на каждую партию в лог летело «не годится».
        proxies_pool = [p for p in (proxies or []) if p and str(p).strip()]
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

    def handle_ip_ban(self, bot, reason: str = "") -> dict:
        """Античит заблокировал IP: адрес в мусор, аккаунты — на другой.

        Блокировка по IP — это не бан аккаунта. С этого адреса сервер не
        пускает никого, но сам аккаунт цел: с другого IP он заходит
        нормально. Поэтому аккаунт не убиваем, а переводим на рабочий
        узел и перезаходим.

        Адрес при этом помечается забаненным навсегда. Без этого пул
        выдаст его следующему боту, тот зайдёт и получит ту же надпись,
        и будет выглядеть, будто сломались аккаунты.

        Возвращает, что сделано: какие ключи забанены, кого перевели.
        """
        proxy = (bot.proxy or "").strip()
        pool = getattr(self, "proxy_pool", None)
        banned_keys: list[str] = []
        if proxy and pool is not None:
            try:
                banned_keys = pool.mark_banned(proxy, reason=reason)
            except Exception as exc:
                print(f"[!] Не смог пометить прокси забаненным: {exc!r}")

        # Все, кто сидел на этом адресе, переезжают на другой. Иначе они
        # будут биться в ту же стену при каждом перезаходе.
        moved: list[int] = []
        marked: list[int] = []
        if proxy:
            for other in list(self.bots.values()):
                if (other.proxy or "").strip() != proxy:
                    continue
                if other.is_banned:
                    continue
                try:
                    other.cancel_reconnect()
                    other.stop()
                except Exception:
                    pass
                new_proxy = self.swap_proxy(other, reason="IP заблокирован античитом")
                if new_proxy:
                    moved.append(other.bot_id)
                else:
                    # Менять не на что: рабочих узлов нет. Тогда честно
                    # помечаем аккаунт, чтобы он не долбился впустую.
                    other.is_banned = True
                    other.ban_reason = f"IP заблокирован, замена не найдена"
                    other.ban_kind = "ip"
                    other.banned_at = time.time()
                    other.status = "ЗАБАНЕН (IP)"
                    other.add_log("[!] Адрес забанен, рабочих узлов для замены нет")
                    marked.append(other.bot_id)
        if moved or marked:
            self.save_accounts()
        print(f"[!] IP-бан: забанен адрес {proxy or '—'}, "
              f"переведено {len(moved)}, помечено {len(marked)}")

        # Отчёт в Telegram: событие на весь адрес, а не на один бот.
        try:
            tg = getattr(self, "tg_controller", None)
            if tg is not None and getattr(tg, "bot", None) is not None:
                owner = getattr(tg, "owner_id", None)
                if owner:
                    lines = [
                        "🚫 <b>АНТИЧИТ ЗАБЛОКИРОВАЛ IP</b>",
                        "",
                        f"Адрес: <code>{proxy or 'свой IP'}</code>",
                        f"Причина: <code>{reason or 'блокировка по IP'}</code>",
                    ]
                    if moved:
                        names = ", ".join(
                            self.bots[i].nickname for i in moved if i in self.bots)
                        lines.append(f"Переведены на другой узел: <b>{len(moved)}</b>")
                        lines.append(f"<i>{names[:300]}</i>")
                    if marked:
                        lines.append(f"Без замены, помечены: <b>{len(marked)}</b>")
                    lines.append("")
                    lines.append("Адрес исключён из пула и больше не выдаётся.")
                    tg.bot.send_message(owner, "\n".join(lines))
        except Exception as exc:
            print(f"[!] Не смог отправить отчёт об IP-бане: {exc!r}")

        return {"proxy": proxy, "banned_keys": banned_keys,
                "moved": moved, "marked": marked}

    def swap_proxy(self, bot, *, reason: str = "") -> str:
        """Заменить боту прокси на рабочий и перезайти.

        Возвращает новый прокси или пустую строку, если менять не на что.
        """
        pool = getattr(self, "proxy_pool", None)
        if pool is None:
            return ""
        current = (bot.proxy or "").strip()
        try:
            candidates = pool.usable_proxies()
        except Exception:
            candidates = []
        fresh = [url for url in candidates if url and url != current]
        if not fresh:
            return ""
        # Берём узел с наименьшей загрузкой. Если всем ботам с одного
        # адреса выдать fresh[0], они переедут на один узел, и сервер
        # увидит ту же кучу аккаунтов с одного IP — только другой.
        load: dict[str, int] = {}
        for other in list(self.bots.values()):
            key = (other.proxy or "").strip()
            if key and key != current:
                load[key] = load.get(key, 0) + 1
        new_proxy = min(fresh, key=lambda url: load.get(url, 0))
        bot.add_log(
            f"[ПРОКСИ] меняю {current or 'свой IP'} → {new_proxy}"
            + (f" ({reason})" if reason else ""))
        try:
            bot.cancel_reconnect()
            bot.stop()
        except Exception:
            pass
        bot.proxy = new_proxy
        bot.proxy_node = self._node_key_for(new_proxy)
        bot._auto_proxy_tried = False
        try:
            self.save_accounts()
        except Exception:
            pass
        time.sleep(1.0)
        try:
            bot.start()
        except Exception as exc:
            bot.add_log(f"[ПРОКСИ] перезаход не удался: {exc!r}")
        return new_proxy

    def _ensure_proxy_watchdog(self) -> None:
        """Фон: упал прокси у работающего бота — меняем и перезаходим.

        Проверка идёт по тем же результатам пула, что и раздача, поэтому
        лишних запросов нет: пул и так обновляет статусы по таймеру.
        """
        if getattr(self, "_proxy_watch", None) is not None:
            return
        stop = threading.Event()

        def _loop():
            while not stop.is_set():
                stop.wait(PROXY_WATCH_INTERVAL)
                if stop.is_set():
                    break
                try:
                    self._proxy_watch_pass()
                except Exception as exc:
                    print(f"[!] Сторож прокси: {exc!r}")

        thread = threading.Thread(target=_loop, name="proxy-watch", daemon=True)
        self._proxy_watch = {"thread": thread, "stop": stop}
        thread.start()

    def _proxy_watch_pass(self) -> None:
        pool = getattr(self, "proxy_pool", None)
        if pool is None:
            return
        now = time.time()
        for bot in list(self.bots.values()):
            if bot.is_banned or bot.stopping:
                continue
            proxy = (bot.proxy or "").strip()
            if not proxy:
                continue
            # Не дёргаем одного бота чаще раза в PROXY_SWAP_COOLDOWN:
            # иначе при общем падении пула начнётся шторм перезаходов.
            last = getattr(bot, "_proxy_swap_at", 0.0)
            if now - last < PROXY_SWAP_COOLDOWN:
                continue
            try:
                if pool.is_banned(proxy):
                    bot._proxy_swap_at = now
                    self.swap_proxy(bot, reason="адрес забанен")
                    continue
                ok, why = pool.verify_before_connect(proxy)
            except Exception:
                continue
            if ok:
                continue
            bot._proxy_swap_at = now
            self.swap_proxy(bot, reason=why or "прокси не отвечает")

    def delete_selected(self, bot_ids: list[int]) -> list[int]:
        """Удалить выбранные аккаунты. Возвращает те, что реально удалены.

        Останавливаем перед удалением: иначе живая сессия останется висеть
        в памяти и продолжит слать пакеты уже без записи в реестре.
        """
        removed = []
        for bot_id in bot_ids:
            with self._lock:
                bot = self.bots.get(bot_id)
            if bot is None:
                continue
            try:
                if getattr(bot, "session", None) is not None:
                    bot.stop()
            except Exception:
                pass
            try:
                self.delete_bot(bot_id)
                removed.append(bot_id)
            except Exception as exc:
                print(f"[!] Удаление #{bot_id}: {exc!r}")
        return removed

    def set_target_level(self, bot_ids: list[int], level) -> list[int]:
        """Поставить цель по уровню пачке аккаунтов.

        level=None или 0 снимает цель. Возвращает те аккаунты, где вышло.
        """
        try:
            target = int(level)
        except (TypeError, ValueError):
            target = 0
        if target < 0:
            target = 0

        changed = []
        for bot_id in bot_ids:
            with self._lock:
                bot = self.bots.get(bot_id)
            if bot is None:
                continue
            try:
                if target <= 0:
                    bot.target_lvl = None
                    bot.target_achieved = False
                else:
                    bot.target_lvl = target
                    # Достигнутость пересчитываем сразу: если бот уже
                    # дотянул до новой цели, ждать следующего PayDay незачем.
                    cur = 0
                    try:
                        cur = int(bot.level_num or 0)
                    except Exception:
                        cur = 0
                    bot.target_achieved = bool(cur and cur >= target)
                changed.append(bot_id)
            except Exception as exc:
                print(f"[!] Цель #{bot_id}: {exc!r}")
        if changed:
            self.save_accounts()
        return changed

    def stop_selected(self, bot_ids: list[int]) -> list[int]:
        """Остановить выбранные аккаунты. Возвращает те, что реально встали."""
        stopped = []
        with self._lock:
            targets = [self.bots[bid] for bid in bot_ids if bid in self.bots]
        for b in targets:
            try:
                self.stop_bot(b.bot_id)
                stopped.append(b.bot_id)
            except Exception as exc:
                print(f"[!] Остановка #{b.bot_id}: {exc!r}")
        return stopped

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
        # Порт моста мог смениться после перезапуска — берём актуальный,
        # иначе бот пойдёт в чужой туннель.
        try:
            bot = self.bots.get(bot_id)
            if bot is not None and getattr(bot, "proxy_node", ""):
                self.refresh_bridge_proxy(bot)
        except Exception as exc:
            print(f"[!] Не смог обновить порт моста для #{bot_id}: {exc!r}")
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
        # Direct-узел отдаёт сам себя, узлу с внешним клиентом поднимается
        # мост sing-box и отдаётся его SOCKS5-вход. Раньше такие узлы
        # просто отбрасывались, и всё, что приходило из подписки, было
        # применить нельзя.
        url = pool.socks_url_for(node)
        if not url:
            return ""
        return url

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
                bot.proxy_node = self._node_key_for(resolved)

        # force: смену прокси нельзя терять. save_accounts дебаунсится на
        # 10 секунд, и при перезапуске сразу после раздачи ключи узлов не
        # успевали записаться — аккаунты оставались с одним портом.
        self.save_accounts(force=True)
        return True

    def _node_key_for(self, proxy: str) -> str:
        """Ключ узла пула за строкой прокси (пусто для ручных прокси)."""
        pool = getattr(self, "proxy_pool", None)
        if pool is None or not proxy:
            return ""
        try:
            node = pool.node_for_proxy(proxy)
        except Exception:
            return ""
        return node.key if node is not None else ""

    def refresh_bridge_proxy(self, bot) -> str:
        """Пересчитать порт моста по ключу узла.

        Порт моста не постоянный: он зависит от того, какие узлы поднялись
        раньше. После перезапуска аккаунт со старым портом смотрел в чужой
        или мёртвый туннель, и бот висел в «сервер не отвечает», хотя узел
        живой. Возвращает актуальную строку прокси.
        """
        key = getattr(bot, "proxy_node", "") or ""
        if not key:
            return bot.proxy or ""
        pool = getattr(self, "proxy_pool", None)
        if pool is None:
            return bot.proxy or ""
        try:
            node = pool.find_node(key)
            if node is None:
                return bot.proxy or ""
            fresh = pool.socks_url_for(node)
        except Exception:
            return bot.proxy or ""
        if fresh and fresh != bot.proxy:
            bot.add_log(f"[ПРОКСИ] порт моста сменился: {bot.proxy} → {fresh}")
            bot.proxy = fresh
        return bot.proxy or ""

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
            node_key = self._node_key_for(resolved)
            for bot in targets:
                bot.proxy = resolved
                bot.proxy_node = node_key
            applied = [bot.bot_id for bot in targets]

        # force: ключи узлов должны лечь на диск сразу, иначе перезапуск
        # сразу после раздачи оставит аккаунты со старыми портами.
        self.save_accounts(force=True)
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

    def _add_admin(current, raw):
        """Добавить id админа к списку. Принимает и один id, и список."""
        ids = list(current or [])
        for piece in str(raw).replace(";", ",").replace(" ", ",").split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except ValueError:
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        return ids

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
            # Админов может быть несколько: --admin 111 --admin 222
            # или --admin 111,222. Раньше каждый следующий флаг затирал
            # предыдущий, и оставался только последний id.
            tg_admin = _add_admin(tg_admin, sys.argv[i + 1])
            i += 2
        elif arg.startswith("--admin="):
            tg_admin = _add_admin(tg_admin, arg.split("=", 1)[1])
            i += 1
        else:
            filtered_args.append(arg)
            i += 1

    # Также проверяем переменную окружения TG_BOT_TOKEN
    if not tg_token:
        tg_token = os.environ.get("TG_BOT_TOKEN")
    if not tg_admin and os.environ.get("TG_ADMIN_ID"):
        tg_admin = _add_admin(None, os.environ["TG_ADMIN_ID"])

    manager = BotManager()

    # Мост веб-панели. Панель — отдельный процесс: она не поднимает второй
    # менеджер и не лезет в accounts.json, иначе два клиента на один ник
    # бились бы за сессию. Здесь только очередь команд и снимок состояния.
    # Запускается до Telegram, чтобы панель видела бота сразу.
    if os.environ.get("BR_WEB_BRIDGE", "1").lower() not in ("0", "false", "no", "off"):
        try:
            from logic.web_bridge import WebBridge

            _web_bridge = WebBridge(manager, log=lambda m: print(m, flush=True))
            _web_bridge.start()
            manager.web_bridge = _web_bridge
        except Exception as _exc:
            print(f"[!] Мост веб-панели не поднялся: {_exc!r}")

    if tg_token:
        print(f"[*] Запуск Telegram бота управления...")
        tg_bot = TelegramBotController(tg_token, manager, admin_id=tg_admin)
        manager.tg_controller = tg_bot
        tg_bot.start()

    # Фоновый режим: запущены с Telegram и без живого терминала.
    #
    # Признак считается здесь и только здесь. Раньше он проверялся лишь
    # перед финальным циклом ожидания, а на пустом реестре управление
    # уходило в интерактивное меню, упиралось в input() без TTY и падало
    # с EOFError — сервис на чистой машине не поднимался вообще.
    background = bool(tg_token) and not sys.stdin.isatty()

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
        elif background:
            print("\n[i] Аккаунтов нет. Добавь их через Telegram-бота: кнопка «➕ Добавить бота».")
        else:
            print("\n[i] Первый запуск. Добавьте аккаунты через меню.")
            interactive_menu(manager)

    print_help()

    # Если запущен в фоне с Telegram ботом (например nohup / daemon без интерактивного TTY)
    if background:
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
