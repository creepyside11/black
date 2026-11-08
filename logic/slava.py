"""Автопрохождение квестов Дяди Славы — движок стадий.

ОТКУДА ВЗЯТО
    libPhoton.so, расшифрованный `.rodata` (XOR-ключ `96316c22826a5560`
    по `0x1b1560`). Именно эти куски и есть тот самый «автопроход»:

        0x1b3000  "Прохожде"        прохождение
        0x1ada91  " дяди Сла"       квестов дяди Славы
        0x1ae7f0  "Дядя Сла"
        0x1ae170  "Маршрут##"       маршрут
        0x1af8f0  "Маршрут #"
        0x1adb00  "Дистанци"        порог пешком/на машине
        0x1b2cc0  "Задержка"        пауза между шагами
        0x1ae530  "Плавност"        плавность движения
        0x1ae200  " ходьбы##B"      ходьба
        0x1b1680  "Сесть в а"       сесть в авто
        0x1b1330  "Выйти из "       выйти из машины
        0x1b1340  "Отъехать"        отъехать
        0x1af4c0  "Ласточка"        та самая машина от Славы
        0x1aeb50  "ласточку"
        0x1b0a00  "Зайти в м"       зайти в маркер
        0x1b09f0  "Ехать на "       ехать на
        0x1b2d30  "Ползунок"        мини-игра
        0x1b0cd0  "Стайлинг"
        0x1b0ce0  "заправку"
        0x1b2fa0  "Коллизии"        обход препятствий
        0x1ae470  "я к Помощ"       помощник
        0x1aeb40  "Помощник"
        0x1b0dd0  "Квест##Auto"

    То есть Photon НЕ патчит память для квестов. Он видит то же, что и мы:
    маркеры, текст цели, диалоги, звонки, мини-игру. И решает две задачи —
    куда идти/ехать и что ответить. Здесь ровно это и собрано.

ТРИ СЛОЯ
    1. Navigator      — пешком или на машине до точки
    2. VehicleRunner  — найти, сесть, завести, выйти
    3. SlavaQuest     — стадии, диалоги, звонки, мини-игра

КАК ЭТО РАБОТАЕТ ВМЕСТЕ
    Сервер присылает маркер (OnSetCheckpoint) -> стадия ставит цель ->
    Navigator выбирает режим по расстоянию (Дистанци) -> едет или идёт ->
    по дороге ловит диалоги/звонки/мини-игру -> приезжает -> маркер
    срабатывает сам, потому что бот внутри радиуса.

Использование:

    from logic.slava import SlavaQuest
    q = SlavaQuest(bot)
    q.start()
    q.on_checkpoint(x, y, z, radius)
    q.on_quest_text(text)
    q.on_npc_dialog(name, text, model, buttons)

СВЕРКА С ЭТАЛОНОМ
    _diag/slava_new.pcap — съёмка PCAPdroid 06.11, сервер TAGANROG, 1065 с.
    Чужой клиент прошёл всю сюжетку «Обучение» целиком: от «Подойти к дяде
    Славе» до подарка новичка. Всё, что ниже помечено «эталон», снято
    оттуда — и последовательность, и паузы, и точные ответы.

    Что делает эталон на каждом шаге:

        маркер (RPC 417)          -> телепорт в его точку (RPC 423)
        цель (iid 39)             -> следующая стадия, лог
        реплика НПС (iid 63)      -> ответ {"bk": <bk из кнопки>} через ~2.1 с
        звонок (iid 65)           -> {"t": 2, "b": 1} через ~2.1 с
        речь по телефону (114)    -> {"c": 1} по истечении d секунд
        всплывашка (iid 13)       -> {"c":13,"t":1,"s":<s>,"b":<b>} через ~3 с
        «пропустить поездку»      -> {"c":13,"t":0,"s":71,"b":<b>} через ~2 с
        меню магазина (iid 10)    -> {"r":1,"i":"<пункт>","l":<индекс>}
        ларёк (iid 3)             -> {"t":1,"r":32} (еда)
        заправка (iid 2)          -> {"f":1,"t":0,"v":1} через ~5 с
        подсказка действия (87)   -> {"c":1} через ~2 с
        ползунок (iid 31)         -> {"t":1} и тут же {"s":1}
        подарок                   -> /donate, iid 127 {"cmd":4,"id":4,"pg":1}

    Телепорт — это RPC 423 с тремя float'ами (x, y, z). Эталон шлёт его
    через 0.09-0.12 с после маркера и попадает ровно в точку маркера.
    Сервер отвечает RPC 448 (SetPlayerPos) — то есть принимает.

    Двигатель в BR включается командой /e, аптечка применяется /healme
    (RPC 378 «server command»), подарок открывается /donate.
"""

from __future__ import annotations

import math
import random
import re
import struct
import threading
import time

# --- интерфейсы --------------------------------------------------------------

QUEST_TASK_IID = 39      # текст цели квеста, поле "mq"
NPC_DIALOG_IID = 63      # реплика НПС, ответ {"bk": <key>}
CALL_IID = 65            # входящий звонок
NOTIFICATION_IID = 13    # «пропустить поездку»
MINIGAME_IID = 31        # ползунок/полоса
SHOP_FOOD_IID = 3        # ларёк

# Остальные интерфейсы — из эталонной записи (06.11, TAGANROG).
NPC_SPEECH_IID = 114     # речь по телефону с таймером d
MENU_IID = 10            # меню магазина/инвентаря
FUEL_IID = 2             # колонка
PROMPT_IID = 87          # подтверждение действия (id = код действия)
DONATE_IID = 127         # донат-меню, оттуда открывается подарок
CASE_REWARD_IID = 128    # окно награды кейса
CASES_IID = 73           # меню кейсов

SHOP_FOOD_ITEM = 32      # id еды в ларьке (эталон покупает именно 32)

# Паузы. Каждая — измеренное расстояние между пакетами в эталоне.
DIALOG_DELAY = (1.9, 2.2)     # 42.347 -> 44.444
CALL_DELAY = (1.9, 2.2)       # 165.089 -> 167.231
SPEECH_TAIL = (0.02, 0.15)    # 176.251 -> 176.273 при d = 9.0
NOTICE_DISMISS = (2.0, 3.6)   # 44.460 -> 47.691, 68.085 -> 71.658
NOTICE_CONFIRM = (1.9, 2.2)   # 649.720 -> 651.750
NOTICE_CONFIRM_ACTION = 0     # поле t в ответе: 0 — подтвердить, 1 — погасить
MENU_PICK_DELAY = (1.9, 2.2)  # 53.980 -> 56.050
SHOP_DELAY = (1.9, 2.2)       # 53.980 -> 56.050
FUEL_DELAY = (4.9, 5.3)       # 443.170 -> 448.390
PROMPT_DELAY = (1.9, 2.2)     # 51.900 -> 53.930
TELEPORT_DELAY = (0.10, 0.35) # 24.030 -> 24.010 (маркер -> RPC 423)
GIFT_DELAY = (1.4, 2.0)       # /donate -> iid 127

# Автоподбор. Эталон добирает пикапы на маркере (канистра — model 1650),
# поэтому радиус берём не 0.5 м, как в api.Autopickup, а по маркеру.
PICKUP_RADIUS = 4.0
PICKUP_TICK = 0.5

# Сколько раз пробуем телепортнуться в одну точку, прежде чем идти пешком.
TELEPORT_TRIES = 3

# Остальное снято с эталона 06.11.
NPC_SPEECH_IID = 114     # речь по телефону с таймером d
MENU_IID = 10            # меню магазина/инвентаря: {"r":1,"i":<пункт>,"l":<idx>}
FUEL_IID = 2             # колонка: {"f":1,"t":0,"v":1}
PROMPT_IID = 87          # подтверждение действия: {"c":1}
DONATE_IID = 127         # донат-меню, оттуда открывается подарок
CASE_REWARD_IID = 128    # окно награды кейса: {"c":1}
CASES_IID = 73           # меню кейсов: {"t":4,"d":1}

SHOP_FOOD_ITEM = 32      # id еды в ларьке (эталон покупает именно 32)

# Паузы. Каждая — измеренное расстояние между пакетами в эталоне.
DIALOG_DELAY = (1.9, 2.2)     # 42.347 -> 44.444
CALL_DELAY = (1.9, 2.2)       # 165.089 -> 167.231
SPEECH_TAIL = (0.02, 0.15)    # 176.251 -> 176.273 при d=9.0
NOTICE_DISMISS = (2.0, 3.6)   # 44.460 -> 47.691, 68.085 -> 71.658
NOTICE_CONFIRM = (1.9, 2.2)   # 649.720 -> 651.750
NOTICE_CONFIRM_ACTION = 0     # поле t в ответе: 0 — подтвердить, 1 — погасить
MENU_PICK_DELAY = (1.9, 2.2)  # 518.263 -> 518.247 (ответ на пункт)
SHOP_DELAY = (1.9, 2.2)       # 53.980 -> 56.050
FUEL_DELAY = (4.9, 5.3)       # 443.170 -> 448.390
PROMPT_DELAY = (1.9, 2.2)     # 51.900 -> 53.930
TELEPORT_DELAY = (0.10, 0.35) # 24.030 -> 24.010 (маркер -> 423)
GIFT_DELAY = (1.4, 2.0)       # /donate -> iid 127

# Автоподбор. Эталон добирает пикапы на маркере (канистра — model 1650),
# поэтому радиус берём не 0.5 м, как в api.Autopickup, а по маркеру.
PICKUP_RADIUS = 4.0
PICKUP_TICK = 0.5

_COLOR_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~")


def normalize(text):
    """Текст цели в вид для сравнения: без цветов, лишних пробелов и регистра."""
    if text is None:
        return ""
    return " ".join(_COLOR_RE.sub("", str(text)).lower().split())


# Точки из эталона: текст цели -> координаты маркера, который пришёл с ней.
# Нужны, когда маркер не дошёл или потерялся, а цель уже есть.
QUEST_ROUTES = {
    "подойти к дяде славе": (2749.43, -2441.77, 21.81),
    "перекусить в уличном киоске": (2744.19, -2429.08, 21.82),
    "вернуться к дяде славе": (2749.43, -2441.77, 21.81),
    "подойти к парковке автомобилей": (2747.91, -2424.07, 21.83),
    "сесть в “ласточку” дяди славы": (2750.61, -2419.56, 21.71),
    'сесть в "ласточку" дяди славы': (2750.61, -2419.56, 21.71),
    "заведите двигатель автомобиля": (2750.61, -2419.56, 21.71),
    "отправиться в магазин '24/7'": (2237.08, -2097.48, 21.00),
    "купить аптечку": (1547.16, 414.35, 1001.04),
    "откройте инвентарь и примените аптечку": (1544.05, 416.30, 1001.04),
    "выйти из магазина": (1544.05, 416.30, 1001.04),
    "сесть в автомобиль": (2307.62, -1917.37, 21.85),
    "отправиться в стайлинг-центр": (2314.32, -2607.41, 20.98),
    "ехать на заправку": (2241.57, -1783.60, 21.76),
    "заправить автомобиль": (2241.57, -1783.60, 21.76),
    "отъехать от заправки": (2199.70, -1786.50, 21.80),
    "купить канистру с бензином": (2241.57, -1783.60, 21.76),
    "войти в магазин '24/7'": (2241.53, -1796.31, 21.76),
    "купить ремонтный набор": (1547.19, 414.24, 1001.04),
    "выйти из магазина '24/7'": (1544.05, 416.30, 1001.04),
    "отправиться в арзамас": (2764.03, -1756.52, 26.31),
    "отправиться к шахте": (2383.58, 1664.53, 13.75),
    "войти в шахту": (2378.81, 1677.46, 15.28),
    "переодеться в рабочую форму": (540.51, 515.35, 1000.32),
    "сдайте руду на ленту": (557.35, 516.12, 999.71),
    "уволиться с работы и сдать форму": (540.41, 515.47, 1000.31),
    "выйти из шахты": (522.74, 517.73, 1000.38),
}

# Что отвечать в меню (iid 10), если пункты не пришли. Эталон шлёт ровно это.
MENU_FALLBACKS = (
    (("аптечк",), {"r": 1, "i": "2. Аптечка", "l": 1}),
    (("ремонтн",), {"r": 1, "i": "6. Ремонтный набор", "l": 5}),
)

MENU_CLOSE = {"r": 1, "i": "", "l": -1}

# --- навигация ---------------------------------------------------------------

# Photon «Дистанци»: ближе — пешком, дальше — на машине.
# 60 м это примерно 12 секунд бега и 4 секунды езды. Граница выбрана так,
# чтобы машину не искать ради перехода через дорогу.
WALK_MAX = 60.0

# Приехали, когда до точки меньше этого. Радиус маркера в BR обычно 2-3 м,
# но сервер засчитывает и чуть раньше — берём с запасом.
ARRIVE_RADIUS = 2.5

# Насколько близко надо подойти к машине, чтобы сесть.
# У setbotvehicle свой лимит 10 м, берём меньше, чтобы не ловить отказ.
BOARD_DISTANCE = 8.0

# Радиус поиска машины вокруг бота.
VEHICLE_SEARCH = 40.0

# Сколько ждать прибытия, прежде чем пересчитать маршрут.
MOVE_TIMEOUT = 90.0

# Пауза после прибытия — человек не выпрыгивает из машины в тот же кадр.
ARRIVE_DELAY = (0.4, 1.0)

# Photon «Задержка»: пауза между шагами стадии.
STEP_DELAY = (0.5, 1.2)

# «Плавность»: параметры движения. coord_delay — как часто шлём позицию.
# Пешком чаще, на машине реже: на скорости редкие пакеты выглядят рывками.
WALK_TUNING = {
    "coord_delay": 0.035,
    "accel_time": 0.6,
    "brake_time": 0.8,
    "completion_distance": ARRIVE_RADIUS,
}
DRIVE_TUNING = {
    "coord_delay": 0.050,
    "accel_time": 1.0,
    "brake_time": 1.4,
    "completion_distance": ARRIVE_RADIUS + 1.0,
}

# --- что ищем в тексте -------------------------------------------------------

# Слова, по которым узнаём, что квест просит сесть за руль.
VEHICLE_WORDS = (
    "сядьте", "садитесь", "сесть", "автомобил", "машин", "ласточк",
    "скутер", "мопед", "транспорт", "за руль", "доехать", "поезжайте",
    "отправляйтесь", "проедьте",
)

# Слова «дойди пешком».
WALK_WORDS = ("подойдите", "идите", "пройдите", "дойдите", "пешком", "зайдите")

# Слова «поговорите с НПС».
TALK_WORDS = ("поговорите", "поговори", "ответьте", "спросите", "вернитесь")

# Кому говорим в сюжетке.
SLAVA_NAMES = ("слав", "дядя")


def _log_to(bot, text):
    try:
        if bot is not None and hasattr(bot, "add_log"):
            bot.add_log(f"[СЛАВА] {text}")
    except Exception:
        pass


def _dist(a, b):
    return math.sqrt(
        (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
    )


def _has(text, words):
    low = str(text or "").lower()
    return any(w in low for w in words)


# --- машина ------------------------------------------------------------------

class VehicleRunner:
    """Сесть, завести, выйти. Всё через то, что уже есть в samp/api.py."""

    def __init__(self, bot, log=None):
        self.bot = bot
        self.api = getattr(bot, "api", None)
        self.log = log or (lambda _m: None)
        self._last_board = 0.0

    # -- состояние --

    def player(self):
        session = getattr(self.bot, "session", None)
        return getattr(getattr(session, "players", None), "bot", None)

    def in_vehicle(self):
        try:
            return bool(self.player().is_in_vehicle())
        except Exception:
            return False

    def current_id(self):
        try:
            return int(self.player().get_current_vehicle_id())
        except Exception:
            return 0

    def position(self):
        try:
            return tuple(self.player().get_position())
        except Exception:
            return (0.0, 0.0, 0.0)

    # -- поиск --

    def vehicles(self):
        session = getattr(self.bot, "session", None)
        stream = getattr(session, "stream", None)
        return getattr(stream, "vehicles", {}) or {}

    def nearest(self, max_distance=VEHICLE_SEARCH):
        """Ближайшая машина в стриме. None — если рядом ничего нет."""
        here = self.position()
        best = None
        best_d = float("inf")
        for vid, veh in list(self.vehicles().items()):
            try:
                d = _dist(here, (float(veh.x), float(veh.y), float(veh.z)))
            except Exception:
                continue
            if d < best_d and d <= float(max_distance):
                best, best_d = veh, d
        return best

    # -- действия --

    def board(self, vehicle=None, *, timeout=6.0):
        """Сесть за руль. True — сидим в машине.

        Сначала подходим пешком: setbotvehicle отказывает дальше 10 м,
        а машина может стоять и в тридцати.
        """
        if self.in_vehicle():
            return True
        if self.api is None:
            return False

        now = time.time()
        if now - self._last_board < 2.0:
            return False
        self._last_board = now

        veh = vehicle or self.nearest()
        if veh is None:
            self.log("машины рядом нет")
            return False

        vid = int(getattr(veh, "vehicle_id", 0) or 0)
        if vid <= 0:
            self.log("у машины нет id")
            return False

        here = self.position()
        target = (float(veh.x), float(veh.y), float(veh.z))
        d = _dist(here, target)

        if d > BOARD_DISTANCE:
            self.log(f"иду к машине {vid} ({d:.1f} м)")
            self._move_to(target, walk=True, arrive=3.0)
            if self.in_vehicle():
                return True
            try:
                veh = self.vehicles().get(vid, veh)
                d = _dist(self.position(), (float(veh.x), float(veh.y), float(veh.z)))
            except Exception:
                pass

        if d > 10.0:
            self.log(f"машина {vid} всё ещё далеко ({d:.1f} м)")
            return False

        try:
            ok = bool(self.api.setbotvehicle(vid, seat_id=0))
        except Exception as exc:
            self.log(f"посадка упала: {type(exc).__name__}: {exc}")
            return False

        if not ok:
            self.log(f"setbotvehicle({vid}) вернул False")
            return False

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if self.in_vehicle():
                self.log(f"сел в машину {vid}")
                self.ensure_engine()
                return True
            time.sleep(0.2)

        self.log(f"после setbotvehicle({vid}) всё ещё пешком")
        return False

    def ensure_engine(self):
        """Завести двигатель. В эталоне это команда /e (RPC 378), не /en."""
        if not self.in_vehicle():
            return False
        try:
            self.api.sendchat("/e")
        except Exception:
            return False
        return True

    def leave(self, *, timeout=4.0):
        """Выйти из машины. True — уже пешком."""
        if not self.in_vehicle():
            return True
        if self.api is None:
            return False
        try:
            self.api.setbotvehicle(0)
        except Exception as exc:
            self.log(f"выход упал: {type(exc).__name__}: {exc}")
            return False

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if not self.in_vehicle():
                self.log("вышел из машины")
                return True
            time.sleep(0.2)
        return False

    def _move_to(self, target, *, walk, arrive):
        # completion_distance лежит И в тюнинге, И здесь. Раньше он уходил
        # дважды и Python падал с «got multiple values for keyword
        # argument» — из-за этого бот никогда не подходил к машине.
        tuning = dict(WALK_TUNING if walk else DRIVE_TUNING)
        tuning["completion_distance"] = float(arrive)
        try:
            self.api.MoveToCoord(
                float(target[0]), float(target[1]), float(target[2]),
                mode="walk" if walk else "vehicle",
                **tuning,
            )
        except Exception as exc:
            self.log(f"движение упало: {type(exc).__name__}: {exc}")
            return False
        return True


# --- навигация ---------------------------------------------------------------

class Navigator:
    """Довести бота до точки. Сам выбирает: пешком или на машине."""

    def __init__(self, bot, *, vehicles=None, log=None):
        self.bot = bot
        self.log = log or (lambda _m: None)
        self.vehicles = vehicles or VehicleRunner(bot, log=self.log)

    def position(self):
        return self.vehicles.position()

    def distance_to(self, target):
        return _dist(self.position(), tuple(target))

    def goto(self, target, *, walk=None, arrive=ARRIVE_RADIUS, timeout=MOVE_TIMEOUT):
        """Дойти или доехать. Возвращает строку-отчёт для лога.

        walk=None — решаем сами по расстоянию (это и есть «Дистанци»).
        """
        if target is None:
            return "нет цели"
        target = (float(target[0]), float(target[1]), float(target[2]))
        d = self.distance_to(target)

        if walk is None:
            walk = d <= WALK_MAX

        # Пешком на 300 м — это минута бега, а на машине двенадцать секунд.
        # Но если машины рядом нет, искать её дольше, чем дойти.
        if not walk and not self.vehicles.in_vehicle():
            veh = self.vehicles.nearest()
            if veh is None:
                self.log(f"машины рядом нет — иду пешком {d:.0f} м")
                walk = True

        if not walk and not self.vehicles.in_vehicle():
            if not self.vehicles.board(veh):
                self.log("сесть не вышло — иду пешком")
                walk = True

        mode = "walk" if walk else "vehicle"
        self.log(f"в путь: {mode}, {d:.1f} м до цели")

        tuning = dict(WALK_TUNING if walk else DRIVE_TUNING)
        tuning["completion_distance"] = (
            float(arrive) if walk else float(arrive) + 1.0
        )
        try:
            self.bot.api.MoveToCoord(
                target[0], target[1], target[2],
                mode=mode,
                **tuning,
            )
        except Exception as exc:
            self.log(f"MoveToCoord упал: {type(exc).__name__}: {exc}")
            return f"движение не запустилось: {exc}"

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            if not getattr(self.bot, "running", True):
                return "остановлено"
            if self.distance_to(target) <= float(arrive) + 0.6:
                time.sleep(random.uniform(*ARRIVE_DELAY))
                return f"на месте ({mode})"
            time.sleep(0.25)

        left = self.distance_to(target)
        return f"не доехал: осталось {left:.1f} м"


# --- перемещение -------------------------------------------------------------

class Teleporter:
    """Доставка бота в точку маркера.

    ЧЕГО НЕ ДЕЛАЕМ: RPC 423 — это СЕРВЕРНЫЙ rpc (set_player_pos). В записи
    06.11 он приходит ОТ сервера 10 раз (скриптовые телепорты сюжета), а
    клиент его никогда не шлёт. Отправка 423 наверх ничего не значит: сервер
    её игнорирует, и бот «стоит» в его глазах, хотя локально уже в точке.

    ЧТО ДЕЛАЕМ: delta-телепорт из `api.DeltaTeleportTo` — путь режется на
    шаги, каждый шаг уходит отдельным sync-пакетом с согласованной velocity
    (8 м/с, как решение владельца). Сервер видит быстрое, но монотонное
    движение и засчитывает подход к маркеру.
    """

    def __init__(self, bot, *, log=None):
        self.bot = bot
        self.log = log or (lambda _m: None)
        self.last_point = None
        self.last_at = 0.0
        self.tries = 0

    @property
    def api(self):
        return getattr(self.bot, "api", None)

    def _player(self):
        session = getattr(self.bot, "session", None)
        return getattr(getattr(session, "players", None), "bot", None)

    def position(self):
        try:
            return tuple(float(v) for v in self._player().get_position())
        except Exception:
            return (0.0, 0.0, 0.0)

    def _local_move(self, x, y, z):
        player = self._player()
        if player is None:
            return
        for name in ("update_position", "_set_position_internal"):
            fn = getattr(player, name, None)
            if fn is None:
                continue
            try:
                fn(float(x), float(y), float(z))
                return
            except Exception:
                continue

    def walk_jump(self, point):
        """Запасной путь: delta-телепорт через api.setbotposition."""
        api = self.api
        if api is None:
            return False
        try:
            api.setbotposition(float(point[0]), float(point[1]), float(point[2]))
        except Exception as exc:
            self.log(f"setbotposition упал: {type(exc).__name__}: {exc}")
            return False
        return True

    def jump(self, point):
        """Delta-телепорт в точку. Возвращает (ok, как)."""
        api = self.api
        if api is None:
            return False, "нет api"
        x, y, z = (float(point[0]), float(point[1]), float(point[2]))

        fn = getattr(api, "DeltaTeleportTo", None)
        if fn is not None:
            try:
                fn(x, y, z)
                return True, "delta 8 м/с"
            except Exception as exc:
                self.log(f"delta-телепорт не вышел: {type(exc).__name__}: {exc}")

        if self.walk_jump((x, y, z)):
            return True, "setbotposition"
        return False, "никак"

    def go(self, point, *, label=""):
        """Довести до точки. Возвращает строку для лога."""
        if point is None:
            return "нет точки"
        x, y, z = (float(point[0]), float(point[1]), float(point[2]))
        here = self.position()
        distance = _dist(here, (x, y, z))
        if distance <= ARRIVE_RADIUS:
            return f"уже на месте ({distance:.1f} м)"

        started = time.time()
        ok, how = self.jump((x, y, z))
        spent = time.time() - started
        self.last_point = (x, y, z)
        self.last_at = time.time()
        if not ok:
            return f"перемещение не удалось ({how})"
        tail = f" {label}" if label else ""
        speed = distance / spent if spent > 0.05 else 0.0
        return (
            f"перемещение{tail}: ({x:.1f}, {y:.1f}, {z:.1f}), "
            f"{distance:.1f} м за {spent:.1f} с ({speed:.1f} м/с), {how}"
        )


# --- стадии ------------------------------------------------------------------

class Stage:
    """Одна стадия квеста.

    hints — слова, по которым узнаём стадию в тексте цели.
    order — чем больше, тем позже проверяется (даёт приоритет точным).
    """

    __slots__ = ("key", "title", "hints", "order", "need_vehicle", "need_walk")

    def __init__(self, key, title, hints, *, order=0,
                 need_vehicle=False, need_walk=False):
        self.key = key
        self.title = title
        self.hints = tuple(hints)
        self.order = int(order)
        self.need_vehicle = bool(need_vehicle)
        self.need_walk = bool(need_walk)

    def matches(self, text):
        low = str(text or "").lower()
        return any(h in low for h in self.hints)

    def __repr__(self):
        return f"Stage({self.key!r})"


# Порядок важен: сверху точные стадии, снизу общие.
# Ключи — латиницей, чтобы не путаться в логах; title — как это видит игрок.
STAGES = (
    Stage("intro", "знакомство со Славой",
          ("поговорите", "поговори", "познаком", "представ", "выслушайте",
           "подойдите к", "подойди к", "подойти к"),
          order=10),
    Stage("board", "сесть в машину",
          ("сядьте", "садитесь", "сесть в", "садитесь в", "ласточк",
           "за руль", "в автомобил", "парковк"),
          order=15),
    Stage("engine", "двигатель",
          ("двигател", "заведите"),
          order=16, need_vehicle=True),
    Stage("food", "покупка еды",
          ("купите", "купіть", "еды", "продукт", "киоск", "ларёк", "перекус"),
          order=20, need_walk=True),
    Stage("shop_enter", "зайти в магазин",
          ("войти в магазин", "зайти в магазин"),
          order=21, need_walk=True),
    Stage("shop_exit", "выйти из магазина",
          ("выйти из магазин", "выходите из магазин"),
          order=22, need_walk=True),
    Stage("use_item", "применить предмет",
          ("примените", "инвентар"),
          order=27),
    Stage("medkit", "аптечка в 24/7",
          ("аптечк", "24/7", "медикамент"),
          order=25, need_vehicle=True),
    Stage("styling", "стайлинг-центр",
          ("стайлинг", "тюнинг", "покрас", "автомастерск", "гараж", "бокс"),
          order=30, need_vehicle=True),
    Stage("fuel", "заправка и канистра",
          ("заправ", "азс", "канистр", "топлив", "бензин"),
          order=35, need_vehicle=True),
    Stage("repair", "ремкомплект",
          ("ремкомплект", "ремонтн", "запчаст"),
          order=36, need_vehicle=True),
    Stage("mine_trip", "поездка на шахту",
          ("шахт", "карьер", "добыч", "рудник"),
          order=40, need_vehicle=True),
    Stage("mine_work", "работа на шахте",
          ("переодень", "роба", "добывайте", "руду", "породу", "ленту",
           "сдайте"),
          order=45, need_walk=True),
    Stage("mine_quit", "увольнение с шахты",
          ("уволь", "рассчита", "закончите работу"),
          order=50, need_walk=True),
    Stage("mine_exit", "выход из шахты",
          ("выйти из шахт", "выходите из шахт"),
          order=55, need_walk=True),
    Stage("return", "возврат на вокзал",
          ("вернитесь", "возвращайтесь", "обратно", "назад к", "возврат",
           "вернуться к"),
          order=60, need_vehicle=True),
    Stage("gift", "подарок новичка",
          ("подарок", "кейс", "донат", "награ", "получить"),
          order=70),
    Stage("final", "финальный разговор",
          ("завершите", "получите награду", "сдайте", "отчитайтесь"),
          order=80),
)

STAGE_BY_KEY = {s.key: s for s in STAGES}

def route_target(text):
    """Координаты маркера для этого текста цели. None — если не знаем."""
    key = normalize(text)
    if key in QUEST_ROUTES:
        return QUEST_ROUTES[key]
    for name, point in QUEST_ROUTES.items():
        if name in key or key in name:
            return point
    return None

# Подарок новичка перед финальным диалогом. Порядок вызовов снят с того,
# что делает игрок руками: Донат -> Подарок -> Кейс -> Забрать -> Закрыть.
# Пауза после каждого — чтобы сервер успел открыть следующее окно.
GIFT_STEPS = (
    ("GetFreeReward", (), 1.5),
    ("CaseOpen", (), 2.0),
    ("CaseCollectReward", (), 1.0),
    ("CaseMenuClose", (), 0.5),
    ("DonatMenuClose", (), 0.5),
)


def match_stage(text):
    """Какая стадия подходит под текст цели. None — не узнали.

    Побеждает САМОЕ ДЛИННОЕ совпадение, а не порядковый номер. Иначе
    «Поговорите с Дядей Славой на вокзале» уезжало в стадию возврата
    только потому, что у неё order больше, и бот шёл сдавать квест,
    который ещё не начал.
    """
    low = str(text or "").lower()
    best = None
    best_score = -1
    for stage in STAGES:
        hits = [h for h in stage.hints if h in low]
        if not hits:
            continue
        score = max(len(h) for h in hits)
        if score > best_score or (score == best_score and best is not None
                                  and stage.order > best.order):
            best, best_score = stage, score
    return best


# --- движок ------------------------------------------------------------------

class SlavaQuest:
    """Полный автопроход квестов Дяди Славы.

    Держит стадию, цель, машину и отвечает на всё, что приходит с сервера.
    Работает в своём потоке; обработчики событий только складывают задачи
    в очередь, чтобы не блокировать разбор пакетов.
    """

    def __init__(self, bot, *, log=None, vehicles=None):
        self.bot = bot
        self.log = log or (lambda m: _log_to(bot, m))
        self.vehicles = vehicles or VehicleRunner(bot, log=self.log)
        self.nav = Navigator(bot, vehicles=self.vehicles, log=self.log)
        self.teleport = Teleporter(bot, log=self.log)

        self.running = False
        self.thread = None

        self.stage = None
        self.quest_text = ""
        self.target = None
        self.target_radius = 3.0
        self.started_at = time.time()

        self.gift_claimed = False
        self.visits = {}
        self.history = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._failures = 0
        self._last_text = ""

        # Чтобы на один интерфейс не уходило по два ответа.
        self._answered = {}
        self._pickup_seen = set()
        self._pickup_at = 0.0
        self._donate_sent = 0.0
        self._gift_clicks = 0
        self._blip_at = 0.0
        self._engine_sent = False
        self._heal_sent = False
        self._menu_seen = 0.0

    # -- управление --

    def start(self):
        if self.running:
            return False
        self.running = True
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.log("автопроход включён")
        return True

    def stop(self):
        if not self.running:
            return False
        self.running = False
        self._wake.set()
        self.log("автопроход выключен")
        return True

    def status(self):
        return {
            "running": self.running,
            "stage": self.stage.key if self.stage else None,
            "stage_title": self.stage.title if self.stage else None,
            "quest": self.quest_text[:120],
            "target": self.target,
            "in_vehicle": self.vehicles.in_vehicle(),
            "visits": dict(self.visits),
            "gift_claimed": self.gift_claimed,
            "elapsed": round(time.time() - self.started_at, 1),
        }

    # -- события --

    def on_quest_text(self, text):
        """Пришла новая цель квеста (iid 39, поле mq)."""
        text = str(text or "").strip()
        if not text or text == self._last_text:
            return False
        # Цель квеста — короткая строка без разделителей. Всё, что похоже на
        # системное сообщение («... | Статистика игрока ...», панель, чат), в
        # цель не годится: раньше такая строка уводила стадию на заправку.
        if len(text) > 90 or "|" in text or "статистика" in normalize(text):
            self.log(f"не цель квеста, игнорирую: {text[:60]!r}")
            return False
        self._last_text = text
        self.quest_text = text
        self.log(f"цель: {text[:110]}")

        stage = match_stage(text)
        point = route_target(text)
        if stage is None and point is None:
            # Не квест: служебная строка, статистика, системное сообщение.
            self.log(f"не цель квеста, игнорирую: {text[:60]!r}")
            return False

        if stage is None:
            self.log("стадию не узнал — жду маркер")

        with self._lock:
            if self.stage is None or stage.key != self.stage.key:
                self.history.append(stage.key)
                self.stage = stage
                self.log(f"стадия -> {stage.title}")

        # Маршрут из эталона: если маркер не придёт, есть куда идти.
        # Свежий маркер (моложе 3 с) важнее таблицы — он от сервера.
        if point is not None and (time.time() - self._blip_at) > 3.0:
            self.target = point
            self.target_radius = ARRIVE_RADIUS
            self.log(f"маршрут из записи: ({point[0]:.1f}, {point[1]:.1f}, {point[2]:.1f})")

        self._quest_actions(stage, text)
        self._wake.set()
        return True

    def _quest_actions(self, stage, text):
        """Что нажать на этой цели, кроме ходьбы. Всё — как в эталоне."""
        low = normalize(text)

        if stage is not None and stage.key == "engine" and not self._engine_sent:
            self._engine_sent = True
            self._delayed(2.0, lambda: self._engine_on(), "завожу двигатель")

        if "примените аптечку" in low and not self._heal_sent:
            self._heal_sent = True
            self._delayed(2.0, lambda: self._command("/healme"), "применяю аптечку")

        if "получить" in low and "подар" in low:
            self._open_gift()

        # Аптечка и ремнабор покупаются пунктом меню iid 10. Эталон шлёт
        # пункт, не дожидаясь списка; если список всё-таки придёт, on_menu
        # ответит сам, а страховка увидит это по _menu_seen и промолчит.
        if "купить" in low and any(w in low for w in ("аптечк", "ремонтн")):
            started = time.time()
            self._delayed(6.0, lambda: self._menu_fallback(started))

    def on_checkpoint(self, x, y, z, radius):
        """Сервер поставил маркер. Это и есть цель движения."""
        try:
            self.target = (float(x), float(y), float(z))
            self.target_radius = max(1.0, float(radius or 3.0))
        except (TypeError, ValueError):
            return False
        self._wake.set()
        return True

    def on_checkpoint_cleared(self):
        self.target = None
        self._wake.set()

    def on_npc_dialog(self, name, text, model=None, buttons=None):
        """Реплика НПС. Отвечаем кнопкой согласия — как Photon."""
        if not self.running:
            return False
        low = str(name or "").lower()
        if not any(n in low for n in SLAVA_NAMES):
            return False

        # Перед финалом Слава ждёт, чтобы подарок был забран.
        if not self.gift_claimed and _has(self.quest_text, ("вернитесь", "назад", "поговорите")):
            self.log("перед финалом забираю подарок новичка")
            self._claim_gift()

        key = self._pick_button(buttons)
        label = self._button_label(buttons, key)
        self.log(
            f"диалог {name}: {str(text or '')[:90]!r} "
            f"(кнопок {len(buttons or [])})"
        )
        self.log(f"жму {label!r} -> bk={key}")
        self._reply_npc(key, name)
        return True

    def on_call(self, header, text, button=None, notif_type=2):
        """Звонок от Славы — принимаем."""
        if not self.running:
            return False
        if not _has(header, SLAVA_NAMES) and not _has(text, SLAVA_NAMES):
            return False
        self.log(f"звонок от {header!r}: {str(text or '')[:90]!r}")
        self._answer_call(notif_type)
        return True

    def on_map_icon(self, icon_id, xyz, marker_type=None, style=None):
        """Маркер квеста (RPC 417). Это и есть точка, куда идёт эталон."""
        if not self.running:
            return False
        try:
            x, y, z = (float(xyz[0]), float(xyz[1]), float(xyz[2]))
        except (TypeError, ValueError, IndexError):
            return False
        self.target = (x, y, z)
        self.target_radius = ARRIVE_RADIUS
        self._blip_at = time.time()
        stage = self.stage.title if self.stage else "без стадии"
        self.log(f"маркер: ({x:.1f}, {y:.1f}, {z:.1f}) — {stage}")
        self._wake.set()
        return True

    def on_dialog_npc(self, duration, text):
        """Речь по телефону (iid 114). Клиент сам закрывает её через d секунд."""
        if not self.running:
            return False
        try:
            d = float(duration or 0.0)
        except (TypeError, ValueError):
            d = 0.0
        self.log(f"речь ({d:.1f} с): {str(text or '')[:90]!r}")
        delay = max(0.1, d) + random.uniform(*SPEECH_TAIL)
        self._delayed(delay, lambda: self._close_json(NPC_SPEECH_IID), "речь закрыта")
        return True

    def on_notification(self, data):
        """Всплывашка (iid 13). Подтверждаемые — подтверждаем, тосты гасим."""
        if not isinstance(data, dict):
            return False
        if data.get("o") != 1:
            return False
        text = str(data.get("i", ""))
        notif_type = data.get("t")
        s = data.get("s", -1)
        b = data.get("b", 0)
        self.log(f"уведомление t={notif_type}: {text[:90]!r} (s={s}, b={b})")

        if _has(text, ("пропустить", "поездк", "дорог")):
            self._skip_trip(data)
            return True

        # Тосты эталон гасит сам, иначе они копятся поверх интерфейса.
        if notif_type in (1, 2, 3, 5):
            delay = 2.4 if notif_type == 1 else random.uniform(*NOTICE_DISMISS)
            self._delayed(
                delay,
                lambda: self._dismiss_notice(s, b),
                f"тост t={notif_type} закрыт",
            )
            return True
        return False

    def on_menu(self, data):
        """Меню iid 10: магазин, инвентарь, покупка. Отвечаем пунктом."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        items = data.get("m")
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        texts = [self._menu_text(item) for item in items]
        self._menu_seen = time.time()
        self.log(f"меню iid=10: {texts[:8]}")

        pick = self._menu_pick(texts)
        if pick is None:
            self._delayed(random.uniform(*MENU_PICK_DELAY),
                          lambda: self._menu_send(MENU_CLOSE), "меню закрыл")
            return True
        self._delayed(random.uniform(*MENU_PICK_DELAY),
                      lambda: self._menu_send(pick), f"выбрал {pick['i']!r}")
        return True

    def on_shop_food(self, data):
        """Ларёк (iid 3). Эталон берёт еду с id 32."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        self._delayed(
            random.uniform(*SHOP_DELAY),
            lambda: self._json_send(SHOP_FOOD_IID, {"t": 1, "r": SHOP_FOOD_ITEM}),
            f"покупаю еду #{SHOP_FOOD_ITEM}",
        )
        return True

    def on_fuel(self, data):
        """Колонка (iid 2). Ответ эталона: {"f":1,"t":0,"v":1}."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        self._delayed(
            random.uniform(*FUEL_DELAY),
            lambda: self._json_send(FUEL_IID, {"f": 1, "t": 0, "v": 1}),
            "заправляю",
        )
        return True

    def on_prompt(self, data):
        """Подсказка действия (iid 87). Клиент подтверждает {"c":1}."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        action_id = data.get("id")
        self.log(f"подтверждение действия id={action_id}")
        self._delayed(
            random.uniform(*PROMPT_DELAY),
            lambda: self._json_send(PROMPT_IID, {"c": 1}),
            "подтвердил",
        )
        return True

    def on_donate(self, data):
        """Донат-меню (iid 127). Отсюда открывается подарок новичка."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        self._gift_clicks += 1
        self.log(f"донат-меню открылось (клик {self._gift_clicks})")
        if self._gift_clicks > 3:
            return True
        self._delayed(
            random.uniform(1.6, 2.2),
            lambda: self._json_send(DONATE_IID, {"cmd": 4, "id": 4, "pg": 1}),
            "жму [Получить] в донат-меню",
        )
        return True

    def on_case_reward(self, data):
        """Окно награды кейса (iid 128): забираем и закрываем."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        self.log(f"награда кейса: {str(data.get('tit', ''))!r}")
        self._delayed(
            random.uniform(*PROMPT_DELAY),
            self._collect_reward,
            "награда забрана",
        )
        return True

    def on_cases(self, data):
        """Меню кейсов (iid 73). Эталон шлёт {"t":4,"d":1}."""
        if not isinstance(data, dict) or data.get("o") != 1:
            return False
        self._delayed(
            random.uniform(*PROMPT_DELAY),
            lambda: self._json_send(CASES_IID, {"t": 4, "d": 1}),
            "кейс открыт",
        )
        return True

    def on_minigame(self, interface_id, data):
        """Мини-игра. Ответ уходит из logic.minigames, здесь только отметка."""
        try:
            if int(interface_id) != MINIGAME_IID:
                return False
        except (TypeError, ValueError):
            return False
        if not isinstance(data, dict) or "g" not in data:
            return False
        self.log(f"мини-игра, режим g={data.get('g')} — ответ ушёл")
        return True

    def on_json(self, interface_id, data):
        """Диспетчер по интерфейсам."""
        try:
            iid = int(interface_id)
        except (TypeError, ValueError):
            return False

        if not isinstance(data, dict):
            return False

        if iid == QUEST_TASK_IID:
            # Только "mq". В этом же интерфейсе прилетают служебные строки
            # (статистика игрока и т.п.), и раньше бот принимал их за цель
            # квеста — стадия прыгала на «заправку» из текста статистики.
            for key in ("mq", "q", "text"):
                if isinstance(data.get(key), str) and data[key].strip():
                    return self.on_quest_text(data[key])
            return False

        if iid == MINIGAME_IID:
            return self.on_minigame(iid, data)

        if iid == NOTIFICATION_IID:
            return self.on_notification(data)

        if iid == NPC_SPEECH_IID:
            duration = data.get("d")
            text = data.get("t")
            if isinstance(text, str) and text:
                return self.on_dialog_npc(duration, text)
            return False

        if iid == MENU_IID:
            return self.on_menu(data)

        if iid == SHOP_FOOD_IID:
            return self.on_shop_food(data)

        if iid == FUEL_IID:
            return self.on_fuel(data)

        if iid == PROMPT_IID:
            return self.on_prompt(data)

        if iid == DONATE_IID:
            return self.on_donate(data)

        if iid == CASE_REWARD_IID:
            return self.on_case_reward(data)

        if iid == CASES_IID:
            return self.on_cases(data)

        return False

    # -- очередь отложенных действий --

    def _delayed(self, delay, fn, note=""):
        """Выполнить через delay секунд, не блокируя разбор пакетов."""
        def _run():
            try:
                time.sleep(max(0.0, float(delay)))
                fn()
                if note:
                    self.log(note)
            except Exception as exc:
                self.log(f"действие не выполнилось: {type(exc).__name__}: {exc}")
        threading.Thread(target=_run, daemon=True).start()

    def _command(self, text):
        """Команда через RPC 378 (server command) — как /e и /healme в эталоне."""
        api = getattr(self.bot, "api", None)
        if api is None:
            return False
        try:
            api.sendchat(text)
            self.log(f"команда {text}")
            return True
        except Exception as exc:
            self.log(f"команда {text} не ушла: {type(exc).__name__}: {exc}")
            return False

    def _engine_on(self):
        if self.vehicles.in_vehicle():
            self._command("/e")
        else:
            self.log("двигатель: не в машине")

    def _json_send(self, iid, payload):
        api = getattr(self.bot, "api", None)
        if api is None:
            return False
        try:
            return bool(api.send_json(int(iid), dict(payload)))
        except Exception as exc:
            self.log(f"iid={iid} не ушёл: {type(exc).__name__}: {exc}")
            return False

    def _close_json(self, iid):
        return self._json_send(iid, {"c": 1})

    def _dismiss_notice(self, s, b):
        return self._json_send(
            NOTIFICATION_IID, {"c": NOTIFICATION_IID, "t": 1, "s": s, "b": b}
        )

    def _menu_send(self, payload):
        return self._json_send(MENU_IID, payload)

    def _collect_reward(self):
        self._json_send(CASE_REWARD_IID, {"c": 1})
        self.gift_claimed = True

    def _menu_fallback(self, started):
        """Меню не открылось — шлём пункт напрямую, как эталон."""
        if self._menu_seen >= started:
            return
        pick = self._menu_pick([])
        if pick is None:
            return
        self._menu_send(pick)
        self.log(f"меню не пришло — шлю пункт {pick['i']!r} напрямую")

    def _menu_text(self, item):
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in ("n", "nm", "t", "i", "name", "text"):
                if isinstance(item.get(key), str) and item[key].strip():
                    return item[key]
        return str(item)

    def _menu_pick(self, texts):
        """Что выбрать в меню под текущую цель. None — закрыть меню."""
        quest = normalize(self.quest_text)
        for words, fallback in MENU_FALLBACKS:
            if any(w in quest for w in words):
                for index, text in enumerate(texts):
                    if any(w in normalize(text) for w in words):
                        return {"r": 1, "i": text, "l": index}
                return dict(fallback)
        return None

    def _button_label(self, buttons, key):
        for b in buttons or []:
            if not isinstance(b, dict):
                continue
            if b.get("bk", b.get("key")) == key:
                return str(b.get("bn") or b.get("text") or "")
        return ""

    # -- поток --

    def _run_loop(self):
        self.log("поток запущен")
        noted = None
        while self.running:
            try:
                if not self._connected():
                    time.sleep(2.0)
                    continue

                self._pickup_tick()

                target = self.target
                if target is None:
                    self._wake.wait(timeout=3.0)
                    self._wake.clear()
                    continue

                arrive = max(ARRIVE_RADIUS, self.target_radius * 0.8)
                distance = self.nav.distance_to(target)
                if distance <= arrive + 0.6:
                    # Маркер засчитывается сам, когда бот внутри радиуса.
                    if noted != target:
                        noted = tuple(target)
                        stage = self.stage.title if self.stage else "точка"
                        self.log(f"на месте: {stage} ({distance:.1f} м)")
                        self._after_arrive()
                    self._wake.wait(timeout=2.0)
                    self._wake.clear()
                    continue

                stage_title = self.stage.title if self.stage else "точка"
                note = self.teleport.go(target, label=f"к цели «{stage_title}»")
                self.log(note)

                if "не удалось" in note:
                    # delta не сработал — дотягиваем обычным движением.
                    walk = self._decide_walk()
                    note = self.nav.goto(target, walk=walk, arrive=arrive)
                    self.log(note)

                # Если движение не запустилось, повторять сразу нельзя:
                # цикл выжигал CPU и забивал лог сотней одинаковых строк.
                if "не запустилось" in note:
                    self._failures += 1
                    time.sleep(min(15.0, 1.5 * self._failures))
                    continue
                self._failures = 0

            except Exception as exc:
                self.log(f"сбой цикла: {type(exc).__name__}: {exc}")
                time.sleep(2.0)
        self.log("поток остановлен")

    def _pickup_tick(self):
        """Автоподбор рядом с ботом. Канистра в эталоне взята именно так."""
        now = time.time()
        if now - self._pickup_at < PICKUP_TICK:
            return
        self._pickup_at = now

        session = getattr(self.bot, "session", None)
        stream = getattr(session, "stream", None)
        pickups = getattr(stream, "pickups", None)
        api = getattr(self.bot, "api", None)
        if not isinstance(pickups, dict) or api is None:
            return

        here = self.teleport.position()
        for pickup in list(pickups.values()):
            try:
                pickup_id = int(getattr(pickup, "pickup_id", -1))
                if pickup_id < 0 or pickup_id in self._pickup_seen:
                    continue
                point = (
                    float(getattr(pickup, "x", 0.0)),
                    float(getattr(pickup, "y", 0.0)),
                    float(getattr(pickup, "z", 0.0)),
                )
                if _dist(here, point) > PICKUP_RADIUS:
                    continue
                model = int(getattr(pickup, "model_id", 0) or 0)
                api.SendPickup(pickup_id)
                self._pickup_seen.add(pickup_id)
                self.log(
                    f"подобрал пикап {pickup_id} (model {model}) "
                    f"в ({point[0]:.1f}, {point[1]:.1f}, {point[2]:.1f})"
                )
            except Exception:
                continue

        if len(self._pickup_seen) > 256:
            self._pickup_seen.clear()

    def _after_arrive(self):
        """Стоим в маркере. Садимся, если просят, выходим, если надо внутрь."""
        time.sleep(random.uniform(*STEP_DELAY))
        stage = self.stage
        if stage is None:
            return
        if self.visits.get(stage.key, 0) == 0:
            self.visits[stage.key] = 1
            self.log(f"на месте: {stage.title}")

        # На стадиях, где надо зайти в помещение/к маркеру пешком,
        # из машины надо выйти — иначе маркер не срабатывает.
        if stage.need_walk and self.vehicles.in_vehicle():
            self.log("нужно зайти внутрь — выхожу из машины")
            self.vehicles.leave()
            return

        # Эталон садится в машину, когда цель — «Сесть в ...»: маркер стоит
        # у машины, и клиент шлёт RPC 382 с её id. Мы тоже.
        if not self.vehicles.in_vehicle() and _has(
            self.quest_text, ("сесть в", "садитесь", "ласточк", "в автомобил")
        ):
            veh = self.vehicles.nearest()
            if veh is None:
                self.log("садиться некуда: машины в радиусе поиска нет")
                return
            self.log(f"сажусь в машину id={getattr(veh, 'vehicle_id', '?')}")
            self.vehicles.board(veh)

    def _decide_walk(self):
        """Пешком или на машине. Учитываем подсказки стадии и расстояние."""
        target = self.target
        if target is None:
            return True
        d = self.nav.distance_to(target)
        stage = self.stage

        if stage is not None and stage.need_walk:
            return True
        if stage is not None and stage.need_vehicle:
            return False
        return d <= WALK_MAX

    # -- вспомогательное --

    def _connected(self):
        session = getattr(self.bot, "session", None)
        return bool(getattr(session, "connected", False))

    def _pick_button(self, buttons):
        """Выбрать кнопку согласия. Порядок как в logic/npc_dialog.py."""
        priority = (
            "далее", "продолжить", "ок", "хорошо", "понял", "готов",
            "да", "взять", "забрать", "согласен", "завершить", "выполнить",
            "уволиться", "уволь", "сдать", "рассчитаться", "получить",
        )
        if not buttons:
            return 1
        best_key = 1
        best_score = -1
        for b in buttons:
            if not isinstance(b, dict):
                continue
            text = str(b.get("bn") or b.get("text") or "").lower()
            key = b.get("bk", b.get("key", 1))
            for index, word in enumerate(priority):
                if word in text:
                    score = len(priority) - index
                    if score > best_score:
                        best_score = score
                        best_key = key
                    break
        return best_key

    def _reply_npc(self, key, name):
        """Ответ на реплику НПС.

        Через `send_npc_dialog_response`: это тот же пакет, что строит
        PacketBuilder.interface_npc_dialog_response, а не самодельный
        send_json — меньше шансов разойтись с клиентом.
        """
        def _send():
            time.sleep(random.uniform(*DIALOG_DELAY))
            api = self.bot.api
            try:
                fn = getattr(api, "send_npc_dialog_response", None)
                if fn is not None:
                    ok = bool(fn(int(key)))
                else:
                    ok = bool(api.send_json(NPC_DIALOG_IID, {"bk": int(key)}))
                self.log(f"ответил {name!r} кнопкой {key} -> {ok}")
            except Exception as exc:
                self.log(f"ответ НПС не ушёл: {type(exc).__name__}: {exc}")
        threading.Thread(target=_send, daemon=True).start()

    def _answer_call(self, notif_type):
        def _send():
            time.sleep(random.uniform(*CALL_DELAY))
            api = self.bot.api
            try:
                fn = getattr(api, "send_call_notification_response", None)
                if fn is not None:
                    ok = bool(fn(int(notif_type or 2), 1))
                else:
                    ok = bool(api.send_json(CALL_IID, {"t": int(notif_type or 2), "b": 1}))
                self.log(f"звонок принят -> {ok}")
            except Exception as exc:
                self.log(f"звонок не принят: {type(exc).__name__}: {exc}")
        threading.Thread(target=_send, daemon=True).start()

    def _skip_trip(self, data):
        # Эталон отвечает ровно тем b, что пришёл в уведомлении (обычно 0),
        # а не единицей: сервер сверяет кнопку.
        button = data.get("b", 0)
        try:
            button = int(button)
        except (TypeError, ValueError):
            button = 0

        def _send():
            time.sleep(random.uniform(*NOTICE_CONFIRM))
            api = self.bot.api
            try:
                fn = getattr(api, "send_notification_response", None)
                if fn is not None:
                    ok = bool(fn(
                        NOTIFICATION_IID,
                        NOTICE_CONFIRM_ACTION,
                        int(data.get("s", 0) or 0),
                        button,
                    ))
                else:
                    ok = bool(api.send_json(
                        NOTIFICATION_IID,
                        {"c": NOTIFICATION_IID, "t": NOTICE_CONFIRM_ACTION,
                         "s": data.get("s", 0), "b": button},
                    ))
                self.log(f"пропустил поездку (b={button}) -> {ok}")
            except Exception as exc:
                self.log(f"пропуск не ушёл: {type(exc).__name__}: {exc}")
        threading.Thread(target=_send, daemon=True).start()

    def _open_gift(self):
        """Подарок: /donate открывает меню, дальше отвечают on_donate/on_cases."""
        now = time.time()
        if now - self._donate_sent < 20.0:
            return
        self._donate_sent = now
        self.log("открываю подарок новичка: /donate")
        self._command("/donate")

    def _claim_gift(self):
        """Подарок новичка: Донат -> Подарок -> Кейс -> Забрать -> Закрыть."""
        def _flow():
            api = self.bot.api
            for name, args, pause in GIFT_STEPS:
                fn = getattr(api, name, None)
                if fn is None:
                    continue
                try:
                    fn(*args)
                    self.log(f"подарок: {name}")
                except Exception as exc:
                    self.log(f"подарок: {name} упал: {exc}")
                time.sleep(pause)
            self.gift_claimed = True
            self.log("подарок забран")
        threading.Thread(target=_flow, daemon=True).start()


def get_quest(bot):
    """Движок на бота, создаётся один раз."""
    quest = getattr(bot, "slava_quest", None)
    if quest is None:
        quest = SlavaQuest(bot)
        bot.slava_quest = quest
    return quest
