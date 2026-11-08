"""Автосбор урожая — то, что в Photon называется Harvester.

ОТКУДА ВЗЯТО
    libPhoton.so, расшифрованный пул строк (ключ XOR лежит по 0x1b1560):

        bHarvest    @0x1b4382   включить автосбор
        bAddSpeed   @0x1b4378   ускорение
        bSkipDiag   @0x1b4388   пропускать диалоги
        DelayMs     @0x1b4391   пауза между циклами
        Harvester   @0x1b2ce6   подпись раздела меню

    Своей логики сбора в клиенте игры НЕТ: слова `harvest` нет ни в
    libblackrussia-client.so (36 МБ), ни в dex, ни в resources.arsc.
    Значит работа целиком серверная: экран приходит диалогом, клиент
    только отвечает. Поэтому и мы ловим диалоги, а не патчим память.

ЧТО ДЕЛАЕТ
    1. ловит меню устройства на работу и выбирает «Сборщик»
    2. ловит подтверждение сбора и жмёт «Собрать»
    3. пропускает служебные диалоги (bSkipDiag)
    4. мини-игру iid 31 закрывает через logic.minigames ({"s":1})
    5. держит паузу между циклами (DelayMs)

ПОЧЕМУ МИНИ-ИГРА ПРОХОДИТСЯ БЕЗ ИГРЫ
    `RunVerticalLineView.isHitted()` — чистая геометрия:
    `x >= mChunk && x <= mChunk + mLineWidth`, где mChunk выбран локальным
    `Random().nextInt(5)`. Клиент отправляет только `{"s":1}` или `{"s":0}`,
    ни позиции, ни зоны, ни времени. Серверу нечем проверить.

Использование:

    from logic.harvester import Harvester
    h = Harvester()
    h.on_dialog(bot, d_id, style, title, info, b1, b2)   # True — ответили
    h.on_minigame(bot, json_data)                        # True — закрыли
"""

from __future__ import annotations

import os
import random
import re
import threading
import time

from logic.menu import find_row, parse_rows, strip_colors

# --- что ищем ---------------------------------------------------------------

# Мини-игра сбора. Ответ уходит в тот же интерфейс.
HARVEST_INTERFACE = 31

# Название работы. Те же строки лежат в logic/farm.py — держим совпадение,
# иначе одно место узнаёт работу, а другое нет.
JOB_NAMES = (
    "сборщик урожая",
    "сборщик фруктов",
    "сборщик",
)

# Экран устройства на работу.
WORK_TITLE_MARKERS = ("работ", "job", "устроиться", "професси")

# Строки списка работ, по которым узнаём, что это тот самый экран.
WORK_LIST_MARKERS = ("сборщик", "начать работу", "начать / закончить")

# Подтверждение сбора — что нажимаем.
# Стемы, а не целые слова: «собрать»/«собрано», «урожай»/«урожая» —
# окончания гуляют, и по целому слову половина экранов не ловилась.
COLLECT_MARKERS = (
    "собра",
    "сорва",
    "собира",
    "урожа",
    "фрукт",
)

# Служебные диалоги, которые Photon пропускает (bSkipDiag).
# Только кнопка, не список: иначе можно случайно выбрать пункт.
SKIP_BUTTON_MARKERS = ("продолжить", "да", "ок", "хорошо", "принять", "забрать")

# Пауза между циклами — аналог DelayMs.
CYCLE_DELAY = (0.6, 1.6)

# На один диалог отвечаем один раз.
DEDUP_WINDOW = 90.0

_KG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:кг|kg)\b", re.IGNORECASE)


def _log(bot, text):
    try:
        if bot is not None and hasattr(bot, "add_log"):
            bot.add_log(f"[АВТОСБОР] {text}")
    except Exception:
        pass


# --- разбор -----------------------------------------------------------------

def parse_kg(text):
    """Сколько килограмм в тексте. None — если числа нет."""
    match = _KG_RE.search(strip_colors(text))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def looks_like_harvest(title, info):
    """Это вообще про сбор урожая?"""
    both = f"{strip_colors(title)} {strip_colors(info)}".lower()
    return any(marker in both for marker in COLLECT_MARKERS)


def looks_like_work_menu(title, info):
    """Экран устройства на работу."""
    head = strip_colors(title).lower()
    body = strip_colors(info).lower()
    if not any(marker in head for marker in WORK_TITLE_MARKERS):
        return False
    return any(marker in body for marker in WORK_LIST_MARKERS)


class HarvestAction:
    """Что сделать с диалогом. Ровно те же поля, что у ответа на диалог."""

    __slots__ = ("kind", "list_item", "response", "text", "note")

    def __init__(self, kind, list_item, response, text, note):
        self.kind = kind          # "pick" — выбор строки, "button" — кнопка
        self.list_item = int(list_item)
        self.response = int(response)
        self.text = text
        self.note = note

    def __repr__(self):
        return (
            f"HarvestAction(kind={self.kind!r}, list_item={self.list_item}, "
            f"response={self.response}, note={self.note!r})"
        )


def decide_harvest(style, title, info, button1="", button2="", *, active=False):
    """Решить, что ответить. None — диалог не наш.

    active — идёт ли сейчас автосбор. Пропуск служебных диалогов
    (bSkipDiag) включается только в этом режиме: иначе бот начнёт
    жать «да» на любом экране игры.
    """
    rows = parse_rows(info, style)
    b1 = strip_colors(button1).lower()

    # 1. Меню устройства на работу -> выбрать «Сборщик».
    if looks_like_work_menu(title, info):
        row = find_row(rows, *JOB_NAMES)
        if row is not None:
            return HarvestAction("pick", row.index, 1, "", f"устроился: {row.text}")

    # 2. Подтверждение сбора.
    #
    # Порядок важен. Кнопка проверяется ПЕРВОЙ: если у диалога есть
    # «Собрать», то текст в теле — это статус («Собрано 7 кг»), а не
    # пункт списка. Иначе find_row цепляется за слово «Собрано» в
    # статусе и отдаёт list_item=0, хотя нажимать надо кнопку.
    if looks_like_harvest(title, info):
        if any(marker in b1 for marker in COLLECT_MARKERS):
            return HarvestAction("button", 0, 1, "", f"сбор: кнопка {b1!r}")

        # Настоящий список: две и больше строки. Тогда ищем пункт.
        if len(rows) >= 2:
            row = find_row(rows, *COLLECT_MARKERS)
            if row is not None:
                return HarvestAction("pick", row.index, 1, "", f"сбор: {row.text}")

        # Одна строка или их нет — это подтверждение, жмём левую.
        return HarvestAction("button", 0, 1, "", "сбор: подтверждаю")

    # 3. Служебные диалоги (bSkipDiag). Только пока идёт автосбор.
    if active and any(marker in b1 for marker in SKIP_BUTTON_MARKERS):
        return HarvestAction("button", 0, 1, "", f"пропуск диалога: {b1!r}")

    return None


# --- драйвер ----------------------------------------------------------------

class Harvester:
    """Ведёт автосбор: отвечает на диалоги и закрывает мини-игру.

    Не запускает потоков сама — вызывается из обработчиков бота.
    Задержку перед ответом выдерживает в отдельном потоке, чтобы не
    блокировать разбор пакетов.
    """

    def __init__(self, *, delay=CYCLE_DELAY, answerer=None, log=None):
        self.delay = delay
        self.answerer = answerer
        self.log = log or (lambda _m: None)
        self.active = False
        self.kg = 0.0
        self.cycles = 0
        self.last_action = None
        self._lock = threading.Lock()
        self._answered = {}

    # -- состояние --

    def start(self, bot=None):
        self.active = True
        self.cycles = 0
        self.kg = 0.0
        _log(bot, "включён")

    def stop(self, bot=None):
        self.active = False
        _log(bot, f"выключен, циклов {self.cycles}, собрано {self.kg:.1f} кг")

    def status(self):
        return {
            "active": self.active,
            "cycles": self.cycles,
            "kg": round(self.kg, 2),
            "last": self.last_action.note if self.last_action else None,
        }

    # -- дедупликация --

    def _seen(self, key):
        now = time.time()
        with self._lock:
            for stale in [k for k, t in self._answered.items() if now - t > DEDUP_WINDOW]:
                self._answered.pop(stale, None)
            return key in self._answered

    def _mark(self, key):
        with self._lock:
            self._answered[key] = time.time()

    # -- диалоги --

    def on_dialog(self, bot, dialog_id, style, title, info,
                  button1="", button2="", *, background=True):
        """Ответить на диалог сбора. True — диалог был наш.

        Пока автосбор не включён, не трогаем НИЧЕГО. Иначе бот, который
        стоит на шахте или заводе, при виде любого меню работ переключился
        бы на сборщика урожая.
        """
        if not self.active:
            return False

        action = decide_harvest(
            style, title, info, button1, button2, active=self.active
        )
        if action is None:
            return False

        self.last_action = action
        self._bump_kg(info)

        key = (int(dialog_id), strip_colors(title)[:120], strip_colors(info)[:400])
        if self._seen(key):
            _log(bot, f"диалог уже отвечен: {action.note}")
            return True

        if background:
            threading.Thread(
                target=self._send, args=(bot, dialog_id, action, key), daemon=True
            ).start()
        else:
            self._send(bot, dialog_id, action, key)
        return True

    def _send(self, bot, dialog_id, action, key):
        # Пауза как у человека: он читает экран, а не щёлкает в тот же кадр.
        if self.delay and self.delay[1] > 0:
            time.sleep(random.uniform(*self.delay))

        session = getattr(bot, "session", None)
        if session is None or not getattr(session, "connected", False):
            _log(bot, "сессия не подключена, ответ не ушёл")
            return False

        answerer = self.answerer or getattr(bot, "menu_answerer", None)
        if answerer is None:
            _log(bot, "нет MenuAnswerer, ответ не ушёл")
            return False

        try:
            ok = answerer.answer(
                session,
                int(dialog_id),
                list_item=action.list_item,
                response=action.response,
                text=action.text,
                note=action.note,
                signature=key,
            )
        except Exception as exc:
            _log(bot, f"ответ упал: {type(exc).__name__}: {exc}")
            return False

        if ok:
            self._mark(key)
            self.cycles += 1
            _log(bot, f"{action.note} (цикл {self.cycles})")
        return bool(ok)

    # -- мини-игра --

    def on_minigame(self, bot, json_data):
        """Закрыть мини-игру сбора. True — это была она.

        Отдельный путь: iid 31 приходит не диалогом, а JSON-интерфейсом,
        и обрабатывается в logic.minigames.
        """
        try:
            mode = json_data.get("g") if isinstance(json_data, dict) else None
        except AttributeError:
            return False
        if mode is None:
            return False

        from logic.minigames import get_solver

        solver = get_solver()
        report = solver.handle(bot, json_data)
        if report.get("sent"):
            self.kg += 1.0
            _log(bot, f"мини-игра закрыта (режим {mode})")
        elif report.get("error"):
            _log(bot, f"мини-игра: {report['error']}")
        return True

    # -- мелочи --

    def _bump_kg(self, info):
        value = parse_kg(info)
        if value is not None and value > self.kg:
            self.kg = value


# Переменная окружения для автостарта. Явно, а не по умолчанию:
# у нас на ферме 317 аккаунтов, и «включено у всех» — это не то, что
# нужно, когда часть ботов стоит на шахте.
ENV_AUTOSTART = "BR_AUTOHARVEST"


def autostart_enabled():
    """Включён ли автосбор переменной окружения."""
    return os.environ.get(ENV_AUTOSTART, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_default = None


def get_harvester():
    global _default
    if _default is None:
        _default = Harvester()
        if autostart_enabled():
            _default.start()
    return _default


def handle_harvest_dialog(bot, dialog_id, style, title, info,
                          button1="", button2=""):
    """Точка входа из обработчика диалогов бота.

    True — диалог относился к сбору и на него ответили.
    """
    harvester = getattr(bot, "harvester", None) or get_harvester()
    return harvester.on_dialog(
        bot, dialog_id, style, title, info, button1, button2
    )
