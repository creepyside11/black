"""Движок качки уровня: от спавна до /bg.

Один заход — ровно та последовательность, которая отработала на живом
сервере:

1. Дождаться, что бот реально в общем мире: рядом есть другие игроки.
   Позиция на вокзале и живой чат миром НЕ считаются — через меню сервер
   отдаёт координаты и чат сразу, а в player stream не пускает, пока не
   уйдёт финальный Spawn.
2. Сказать в лог, на каком вокзале бот оказался.
3. Выдержать паузу после спавна: сервер только что высадил персонажа.
   Пауза каждый раз разная — одинаковые 60.0 с у всех аккаунтов выдают
   их уже самим совпадением.
4. Взять точку для этого вокзала. На вокзал до трёх точек, выбор
   случайный и без повтора — десяток аккаунтов не встаёт в одну клетку.
5. Перейти на точку delta-методом: шаг в пакете равен speed * interval,
   античит видит бег, а не прыжок. Скорость берётся из TRAVEL_SPEED_RANGE
   и не превышает потолок, который сервер считает честным.
6. Прописать /bg.

Бессмертия здесь нет намеренно. Держать HP на сотне круглосуточно — это
ровная константа, которой у живого игрока не бывает: голод, урон и
восстановление дают колебания. Такой признак копится и приводит к бану
не сразу, а через несколько часов — у каждого аккаунта в свой момент.

Координаты лежат в logic/lvl_targets.json. Для вокзала без точек движок
честно сообщает об этом и остаётся на вокзале: в ноль он не телепортируется.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time

TARGETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lvl_targets.json")

# Сколько ждать после спавна перед переходом на точку.
SPAWN_DELAY = 60.0
# Разброс паузы. Пятнадцать ботов, выходящих на точку ровно через 60.0 с
# после спавна, выдают себя уже самим совпадением — у живых игроков так не
# бывает. Каждому заходу достаётся своё значение из диапазона.
SPAWN_DELAY_RANGE = (52.0, 78.0)
# Сколько ждать подтверждения мира по стриму игроков.
WORLD_TIMEOUT = 40.0
# Команда после прибытия.
ARRIVAL_COMMAND = "/bg"

# Скорость перехода. 8 м/с — под потолком, который сервер считает честным:
# позиция и velocity в пакете сходятся, античит видит обычный бег.
# Раньше здесь был pause_warp кусками по 45 м с паузой 0.4 с — это 112 м/с,
# сервер такие прыжки откатывал, и бот оставался на вокзале.
TRAVEL_SPEED = 8.0
# Рабочий диапазон скорости. Ровные 8.0 м/с на всём пути — тоже признак:
# живой игрок то ускоряется, то сбрасывает. Потолок не превышаем.
TRAVEL_SPEED_RANGE = (6.6, 8.0)
TRAVEL_INTERVAL = 0.02
# Небольшое дрожание шага. Без него линия пути идеально прямая.
TRAVEL_JITTER = 0.12

# Прибытие считается состоявшимся в этих пределах.
ARRIVE_RADIUS = 5.0

def _pick(bounds: tuple[float, float], rng: random.Random | None = None) -> float:
    """Случайное значение из диапазона.

    `rng` принимается, чтобы тесты могли зафиксировать результат; в работе
    передаётся None и берётся обычный random.
    """
    low, high = bounds
    source = rng or random
    return float(source.uniform(float(low), float(high)))


def spawn_delay_value(rng: random.Random | None = None) -> float:
    """Пауза после спавна для одного захода."""
    return _pick(SPAWN_DELAY_RANGE, rng)


def travel_speed_value(rng: random.Random | None = None) -> float:
    """Скорость перехода для одного захода."""
    return _pick(TRAVEL_SPEED_RANGE, rng)


# Последняя выбранная точка на бота: две подряд одинаковые — заметно.
_LAST_PICK: dict[int, int] = {}
_PICK_LOCK = threading.Lock()


def _bot_id(bot) -> int:
    try:
        return int(getattr(bot, "bot_id", 0) or 0)
    except Exception:
        return 0


# --- координаты -------------------------------------------------------------


def load_targets(path: str | None = None) -> dict:
    """Все точки по вокзалам. Битый файл не роняет бота."""
    target = path or TARGETS_FILE
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    spawns = data.get("spawns") if isinstance(data, dict) else None
    if not isinstance(spawns, dict):
        return {}
    clean: dict[str, list[tuple[float, float, float]]] = {}
    for name, points in spawns.items():
        if not isinstance(points, list):
            continue
        valid = []
        for point in points:
            try:
                x, y, z = (float(v) for v in point[:3])
            except Exception:
                continue
            valid.append((x, y, z))
        clean[str(name)] = valid
    return clean


def save_targets(spawns: dict, path: str | None = None) -> None:
    target = path or TARGETS_FILE
    payload = {
        "_comment": "Точки для качки уровня: у каждого спавна до трёх вариантов. "
                    "Бот берёт случайную.",
        "spawns": {name: [list(p) for p in points]
                   for name, points in spawns.items()},
    }
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, target)


def pick_target(bot, spawn_name: str, *, spawns: dict | None = None,
                rng: random.Random | None = None):
    """Случайная точка для вокзала, не та же, что в прошлый раз.

    Возвращает (index, point) или (None, None), если точек нет.
    """
    table = spawns if spawns is not None else load_targets()
    points = table.get(spawn_name) or []
    if not points:
        return None, None
    chooser = rng or random
    with _PICK_LOCK:
        key = _bot_id(bot)
        last = _LAST_PICK.get(key)
    if len(points) == 1:
        index = 0
    else:
        choices = [i for i in range(len(points)) if i != last] or list(range(len(points)))
        index = chooser.choice(choices)
    with _PICK_LOCK:
        _LAST_PICK[key] = index
    return index, points[index]


def current_station(bot) -> str | None:
    """Название вокзала, на котором стоит бот."""
    try:
        pos = bot.api.getbotposition() if bot.api else None
    except Exception:
        pos = None
    if not pos or len(pos) < 3:
        return None
    try:
        from logic.spawn import nearest_spawn_name
        name, _dist = nearest_spawn_name(pos)
        return name
    except Exception:
        return None


def status(bot) -> dict:
    spawn = current_station(bot)
    table = load_targets()
    points = table.get(spawn) or [] if spawn else []
    return {
        "station": spawn,
        "points": len(points),
        "target": getattr(bot, "lvl_target", None),
        "phase": getattr(bot, "lvl_phase", ""),
    }


# --- переход ----------------------------------------------------------------


def travel_to_point(bot, point, *, speed: float | None = None, log=None) -> dict:
    """Дойти до точки обычным delta-переходом на честной скорости.

    Тот же механизм, что и travel_to_station: пачка коротких приращений по
    OnFootSync, где шаг равен speed * interval. Сервер видит монотонное
    движение, а не прыжок, и позицию не откатывает.

    Скорость по умолчанию берётся случайной из TRAVEL_SPEED_RANGE: держать
    на всём пути ровно 8.0 м/с — значит идти с постоянной скоростью, чего
    живой игрок не делает.
    """
    from logic.delta_teleport import DeltaTeleport, step_for_speed

    api = getattr(bot, "api", None)
    session = getattr(bot, "session", None)
    if api is None or session is None:
        return {"status": "no_api", "left": None}
    if not getattr(session, "connected", False):
        return {"status": "no_session", "left": None}

    if speed is None:
        speed = travel_speed_value()
    speed = float(speed)

    engine = DeltaTeleport(
        session.players.bot,
        api._delta_sync_sender,
        step=step_for_speed(speed, TRAVEL_INTERVAL),
        interval=TRAVEL_INTERVAL,
        velocity_mode="consistent",
        jitter=TRAVEL_JITTER,
        settle_packets=1,
        abort_check=lambda: not session.connected,
    )

    was_enabled = bool(getattr(session.sync, "enabled", False))
    if was_enabled:
        session.sync.stop()
    try:
        result = engine.run(tuple(float(v) for v in point))
    finally:
        if was_enabled and session.connected:
            session.sync.start()

    if log:
        log(
            f"[LVL] переход {result.get('status')}: "
            f"{result.get('distance', 0):.0f}м за {result.get('elapsed', 0):.0f}с "
            f"({speed:.1f} м/с)"
        )
    return result


# --- один заход -------------------------------------------------------------


def run_cycle(
    bot,
    *,
    log=None,
    spawn_delay: float | None = None,
    world_timeout: float = WORLD_TIMEOUT,
    command: str = ARRIVAL_COMMAND,
    spawns: dict | None = None,
    rng: random.Random | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Полный заход: мир → вокзал → пауза → точка → /bg.

    Возвращает отчёт. Пока идёт заход, на боте поднят флаг
    `_lvl_engine_active`: фоновая проверка мира не должна вести ту же
    сессию своим переходом, иначе два потока дёргают позицию в разные
    стороны и стрим остаётся пустым.

    Пауза после спавна не задана — берётся случайная из SPAWN_DELAY_RANGE.
    """
    say = log or (lambda _m: None)
    if spawn_delay is None:
        spawn_delay = spawn_delay_value(rng)
    stop = stop_event or threading.Event()
    started = time.monotonic()

    def stopped() -> bool:
        return stop.is_set()

    bot._lvl_engine_active = True
    bot.lvl_phase = "world"
    try:
        # 1. Мир.
        from logic.real_world import ensure as enter_real_world

        world = enter_real_world(
            bot, timeout=world_timeout, fallback_warp=False, log=say,
        )
        if world.get("status") == "disconnected":
            say("[LVL] соединение разорвалось во время проверки мира; ждём переподключения")
            return {"status": "disconnected", "station": None, "players": 0}
        station = current_station(bot) or world.get("station")
        bot.lvl_spawn_name = station
        players = world.get("players") or []

        if world.get("status") != "confirmed":
            say(f"[LVL] мир не подтверждён, стою на «{station or '?'}»")
            return {
                "status": "world_unconfirmed",
                "station": station,
                "players": len(players),
                "spawn": world.get("spawn"),
                "seconds": round(time.monotonic() - started, 1),
            }

        # 2. Вокзал в лог.
        say(f"[LVL] СПАВН: «{station or 'неизвестно'}» | рядом игроков: {len(players)}")
        if stopped():
            return {"status": "stopped", "station": station}

        # 3. Минута после спавна.
        bot.lvl_phase = "delay"
        say(f"[LVL] жду {spawn_delay:.0f}с после спавна")
        left_delay = float(spawn_delay)
        while left_delay > 0 and not stopped():
            step = min(5.0, left_delay)
            stop.wait(step)
            left_delay -= step
        if stopped():
            return {"status": "stopped", "station": station}

        # 4. Точка своего вокзала.
        bot.lvl_phase = "target"
        index, point = pick_target(bot, station, spawns=spawns, rng=rng)
        if point is None:
            say(
                f"[LVL] для «{station}» нет координат — стою на вокзале. "
                f"Заполни logic/lvl_targets.json"
            )
            return {"status": "no_targets", "station": station}

        bot.lvl_target = list(point)
        bot.lvl_target_index = index
        say(f"[LVL] точка {index + 1}: ({point[0]:.1f}, {point[1]:.1f}, {point[2]:.1f})")

        # 5. Переход на честной скорости.
        bot.lvl_phase = "moving"
        try:
            moved = travel_to_point(bot, point, log=say)
        except Exception as exc:
            say(f"[LVL] переход не удался: {exc!r}")
            return {"status": "warp_failed", "error": repr(exc), "station": station}

        final = moved.get("final_position")
        if final is None:
            try:
                final = bot.api.getbotposition()
            except Exception:
                final = None
        if final is None:
            left = float("inf")
        else:
            left = math.dist(tuple(float(v) for v in final), point)
        if left > ARRIVE_RADIUS:
            say(f"[LVL] не дошёл, осталось {left:.1f}м")
            return {
                "status": "short",
                "left": left,
                "station": station,
                "target": list(point),
            }

        say(f"[LVL] на точке, остаток {left:.2f}м")
        if stopped():
            return {"status": "stopped", "station": station, "target": list(point)}

        # 6. Команда.
        bot.lvl_phase = "command"
        sent = False
        try:
            if bot.api:
                bot.api.sendchat(command)
                sent = True
                say(f"[LVL] отправил {command}")
        except Exception as exc:
            say(f"[LVL] команда не ушла: {exc!r}")

        bot.lvl_phase = "done"
        return {
            "status": "done",
            "station": station,
            "target": list(point),
            "index": index,
            "players": len(players),
            "command": command,
            "command_sent": sent,
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        bot._lvl_engine_active = False


class LevelRun:
    """Тот же заход, но в своём потоке — для задачи на боте."""

    def __init__(self, bot, *, log=None, spawn_delay: float | None = None,
                 world_timeout: float = WORLD_TIMEOUT, spawns: dict | None = None,
                 command: str = ARRIVAL_COMMAND, rng: random.Random | None = None):
        self.bot = bot
        self.log = log or (lambda _m: None)
        # Не задана — берём случайную из SPAWN_DELAY_RANGE. Явное число
        # остаётся для тестов и ручных прогонов.
        self.spawn_delay = (
            float(spawn_delay) if spawn_delay is not None
            else spawn_delay_value(rng)
        )
        self.world_timeout = float(world_timeout)
        self.spawns = spawns
        self.command = command
        self.rng = rng
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.report: dict = {"status": "idle"}

    def start(self) -> "LevelRun":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"lvl-run-{_bot_id(self.bot)}", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_once(self) -> dict:
        """Выполнить заход в текущем потоке."""
        self.report = self._run_cycle()
        return self.report

    def _run_cycle(self) -> dict:
        try:
            return run_cycle(
                self.bot,
                log=self.log,
                spawn_delay=self.spawn_delay,
                world_timeout=self.world_timeout,
                command=self.command,
                spawns=self.spawns,
                rng=self.rng,
                stop_event=self._stop,
            )
        except Exception as exc:
            self.log(f"[LVL] движок упал: {exc!r}")
            return {"status": "error", "error": repr(exc)}

    def _run(self) -> None:
        self.report = self._run_cycle()
