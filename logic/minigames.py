"""Мини-игры Black Russia — интерфейс 31.

Снято с байткода `GUIMiniGameEventsGame` и его лейаутов
(classes4.dex, пакет `com.blackhub.bronline.game.gui.minigameevents`).

ЭКРАН
    JSON интерфейс 31. `GUIManager.createGuiFromId(31)` возвращает
    `GUIMiniGameEventsGame.newInstance()` — проверено декодированием
    packed-switch по сырым байтам.

РЕЖИМ ВЫБИРАЕТСЯ КЛЮЧОМ `g`
    show() читает `g` (optInt) и `x` (optInt), затем добавляет 4 лейаута
    по индексу:

        g = 0  ->  UILayoutMiniGameEventsGamePrize    экран приза
        g = 1  ->  UILayoutMiniGameEventsGameUnTie    развязывание
        g = 2  ->  UILayoutMiniGameEventsGameFinger   полоса/линия
        g = 3  ->  UILayoutMiniGameEventsGameImage    картинка

ОТВЕТЫ (все — в тот же интерфейс 31)
    {"s": 1}  попадание        UILayoutMiniGameEventsGameFinger
    {"s": 0}  промах           UILayoutMiniGameEventsGameFinger
    {"t": 1}  время вышло      UILayoutMiniGameEventsGameUnTie
    {"c": 1}  закрыть          GUIMiniGameEventsGame.onCreateView

ГЛАВНОЕ ПРО АВТОПРОХОЖДЕНИЕ
    Попадание клиент определяет САМ, локально:
    `RunVerticalLineView.isHitted()` сравнивает x бегущей линии с mChunk
    и mLineWidth — чистая геометрия, никаких серверных данных.
    Дальше он просто сообщает результат: `{"s":1}` или `{"s":0}`.

    Значит, чтобы пройти не играя, достаточно послать `{"s":1}` сразу,
    как пришёл интерфейс 31 с `g = 2`. Серверу нечем это опровергнуть:
    единственный источник вердикта — клиент.

ВХОДЯЩИЕ КЛЮЧИ (все, что читает клиент)
    g         выбор лейаута, см. выше
    x         доп. число для лейаута
    t         тип
    h n r m s числа
    q1 q2 q3 z  optDouble, геометрия
    p_start p_max p_max_bar p_tick p_timer p_title
              параметры полосы прогресса

Использование:

    from logic.minigames import handle_minigame
    handle_minigame(bot, interface_id, json_data)   # True, если это была мини-игра
"""

from __future__ import annotations

import random
import threading
import time

# --- протокол ---------------------------------------------------------------

MINIGAME_INTERFACE = 31

KEY_MODE = "g"
KEY_EXTRA = "x"
KEY_TYPE = "t"

# Ответы. Значения — ровно те, что шлёт клиент.
ANSWER_HIT = {"s": 1}
ANSWER_MISS = {"s": 0}
ANSWER_TIMEOUT = {"t": 1}
ANSWER_CLOSE = {"c": 1}

MODE_PRIZE = 0
MODE_UNTIE = 1
MODE_FINGER = 2
MODE_IMAGE = 3

MODE_NAMES = {
    MODE_PRIZE: "приз",
    MODE_UNTIE: "развязывание",
    MODE_FINGER: "полоса",
    MODE_IMAGE: "картинка",
}

# Пауза перед ответом. Клиент отвечает, когда линия доходит до цели, то есть
# через некоторое время после открытия, а не мгновенно. Мгновенный ответ
# выглядит неестественно.
HIT_DELAY = (0.8, 2.2)

# Ползунок эталон закрывает сразу парой пакетов: {"t":1} (время вышло) и
# следом {"s":1} (попадание). В _diag/slava_new.pcap оба уходят в одну
# миллисекунду — сервер считает попадание по ним, а не по геометрии.
SLIDER_DELAY = (0.0, 0.05)

# На одну и ту же мини-игру отвечаем один раз.
DEDUP_WINDOW = 120.0

# Ключи полосы прогресса. Их наличие = ползунок, а не «полоса на попадание».
SLIDER_KEYS = ("p_title", "p_max", "p_start", "p_max_bar", "p_tick", "p_timer")


def _log(bot, text):
    try:
        if bot is not None and hasattr(bot, "add_log"):
            bot.add_log(f"[МИНИ-ИГРА] {text}")
    except Exception:
        pass


# --- разбор -----------------------------------------------------------------

def parse_mode(json_data):
    """Режим мини-игры из ключа `g`. None — если это не мини-игра."""
    if not isinstance(json_data, dict):
        return None
    value = json_data.get(KEY_MODE)
    if not isinstance(value, (int, float)):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
    return int(value)


def is_minigame_packet(interface_id, json_data):
    """Это пакет мини-игры? Проверка по настоящему протоколу.

    Раньше в задачах (`bot_tasks.py`) мини-игру искали по ключам
    "slider", "green_zone", "cursor", "target_zone", "target" и по
    `data["t"] in (1,2) and "gz" in data`. Ни одного из них в протоколе
    нет — экран опознаётся по `g`, и только так.
    """
    try:
        if int(interface_id) != MINIGAME_INTERFACE:
            return False
    except (TypeError, ValueError):
        return False
    if not isinstance(json_data, dict):
        return False
    return parse_mode(json_data) is not None


def describe(json_data):
    """Короткое описание пакета для логов."""
    if not isinstance(json_data, dict):
        return str(json_data)[:120]
    mode = parse_mode(json_data)
    name = MODE_NAMES.get(mode, f"режим {mode}")
    title = json_data.get("p_title")
    parts = [f"g={mode} ({name})"]
    if title:
        parts.append(f"p_title={str(title)[:40]!r}")
    if "x" in json_data:
        parts.append(f"x={json_data.get('x')}")
    return ", ".join(parts)


def is_slider(json_data):
    """Это ползунок? В эталоне он приходит как g=1 с ключами p_*."""
    if not isinstance(json_data, dict):
        return False
    if parse_mode(json_data) != MODE_UNTIE:
        return False
    return any(key in json_data for key in SLIDER_KEYS)


def is_progress(json_data):
    """Полоса прогресса между шагами ползунка: g=2 без геометрии.

    На неё клиент НЕ отвечает — эталон просто ждёт следующий экран.
    Раньше мы отвечали {"s":1} и сбивали состояние.
    """
    if not isinstance(json_data, dict):
        return False
    if parse_mode(json_data) != MODE_FINGER:
        return False
    return "q1" not in json_data and "m" not in json_data


def answers_for(json_data):
    """Список ответов на экран. Пустой — не отвечаем вообще."""
    mode = parse_mode(json_data)
    if mode is None:
        return []
    if is_slider(json_data):
        return [dict(ANSWER_TIMEOUT), dict(ANSWER_HIT)]
    if is_progress(json_data):
        return []
    if mode == MODE_FINGER:
        return [dict(ANSWER_HIT)]
    if mode == MODE_UNTIE:
        # Не ползунок, а развязывание: время вышло, закрываем.
        return [dict(ANSWER_TIMEOUT), dict(ANSWER_CLOSE)]
    return [dict(ANSWER_CLOSE)]


def answer_for_mode(mode):
    """Совместимость: что отвечать на этот режим при автопрохождении."""
    if mode == MODE_FINGER:
        return dict(ANSWER_HIT)
    if mode == MODE_PRIZE:
        # Экран приза закрывается тем же {"c":1}.
        return dict(ANSWER_CLOSE)
    if mode == MODE_UNTIE:
        return dict(ANSWER_CLOSE)
    return dict(ANSWER_CLOSE)


# --- обработчик -------------------------------------------------------------

class MinigameSolver:
    """Отвечает на мини-игры. Один ответ на один экран."""

    def __init__(self, *, delay=HIT_DELAY, autopass=True):
        self.delay = delay
        self.autopass = autopass
        self._lock = threading.Lock()
        self._answered = {}
        self.last = None

    def _seen(self, key):
        with self._lock:
            now = time.time()
            for k in [k for k, t in self._answered.items() if now - t > DEDUP_WINDOW]:
                self._answered.pop(k, None)
            return key in self._answered

    def _mark(self, key):
        with self._lock:
            self._answered[key] = time.time()

    def handle(self, bot, json_data, *, dry_run=False):
        mode = parse_mode(json_data)
        report = {"mode": mode, "answer": None, "sent": False, "error": None}

        if mode is None:
            report["error"] = "нет ключа g"
            return report

        if is_progress(json_data):
            report["error"] = "полоса прогресса — не отвечаем"
            return report

        # Ключ дедупликации: режим + название + x + стартовая позиция.
        # Ползунок приходит дважды (p_start 0 и 50) — без p_start второй
        # экран считался бы повтором и оставался без ответа.
        key = (
            mode,
            str(json_data.get("p_title", "")),
            str(json_data.get(KEY_EXTRA, "")),
            str(json_data.get("p_start", "")),
            str(json_data.get("p_max", "")),
        )
        if self._seen(key):
            report["error"] = "на эту мини-игру уже отвечали"
            return report

        answers = answers_for(json_data)
        report["answer"] = answers[0] if answers else None
        report["answers"] = answers
        if not answers:
            report["error"] = "отвечать не нужно"
            return report

        if dry_run:
            _log(bot, f"сухой прогон: {describe(json_data)} -> {answers}")
            return report

        slider = is_slider(json_data)
        if not slider and self.delay and self.delay[1] > 0:
            time.sleep(random.uniform(*self.delay))

        session = getattr(bot, "session", None)
        if session is None or not getattr(session, "connected", False):
            report["error"] = "сессия не подключена"
            return report

        sent_count = 0
        for index, answer in enumerate(answers):
            if index and slider:
                time.sleep(random.uniform(*SLIDER_DELAY))
            try:
                ok = session.send_json(MINIGAME_INTERFACE, answer)
            except Exception as exc:
                report["error"] = f"отправка упала: {type(exc).__name__}: {exc}"
                _log(bot, report["error"])
                return report
            if not ok:
                report["error"] = "send_json вернул False"
                return report
            sent_count += 1

        report["sent"] = sent_count == len(answers)
        if report["sent"]:
            self._mark(key)
            _log(bot, f"{describe(json_data)} -> {' + '.join(str(a) for a in answers)}")
        self.last = report
        return report


_default_solver = None


def get_solver():
    global _default_solver
    if _default_solver is None:
        _default_solver = MinigameSolver()
    return _default_solver


def handle_minigame(bot, interface_id, json_data, *, background=True):
    """Точка входа из обработчика JSON.

    True — если пакет относился к мини-игре (даже если ответ не ушёл).
    """
    try:
        if int(interface_id) != MINIGAME_INTERFACE:
            return False
    except (TypeError, ValueError):
        return False

    solver = get_solver()
    if background:
        threading.Thread(
            target=solver.handle, args=(bot, json_data), daemon=True
        ).start()
    else:
        solver.handle(bot, json_data)
    return True
