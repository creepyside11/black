"""Человеческое поведение для АФК-бота.

Задача — не «спрятать бота от античита», а не давать повода его искать.
Администратор банит по тому, что видит глазами, наблюдая за игроком:

    стоит девять минут без движения
    → шагает полтора метра в случайную сторону
    → снова замирает на девять минут
    → ни разу не повернул голову
    → 24/7 в сети без единого перерыва

Ровно это и делала старая качалка. Ниже — набор действий, которые
выглядят как живой игрок, которого оставили в игре: он ходит по делам,
осматривается, стоит, садится, иногда уходит насовсем на полчаса.

Ключевое отличие от прежнего кода — не «случайный шаг раз в N секунд»,
а последовательность осмысленных действий с разной длительностью.
"""

from __future__ import annotations

import math
import random
import time

from logic.movement import MAX_STEP_DT

# Скорости живого игрока в SA-MP, м/с.
WALK_SPEED = (1.15, 1.45)
JOG_SPEED = (1.9, 2.4)


def coord_delay_for_speed(speed_mps: float, sync_interval: float) -> float:
    """coord_delay, при котором бот реально пойдёт с нужной скоростью.

    В CoordMovementController шаг считается как

        step_distance = coord_delay * (dt / sync_interval)

    где dt — время с прошлого sync-пакета, но не больше MAX_STEP_DT.
    Значит скорость:

        v = coord_delay * min(sync, MAX_STEP_DT) / sync²

    Отсюда обратная формула. Это важно: в ECO-режиме sync раз в 0.8 с,
    а dt режется до 0.25 — то есть бот проходит лишь треть заложенного
    пути. Старая качалка передавала coord_delay=0.03, и в ECO это давало
    1.2 см/с: бот не шёл, а дрожал на месте.
    """
    sync = max(0.05, float(sync_interval))
    step = min(sync, MAX_STEP_DT)
    if step <= 0.0:
        return 0.95
    return max(0.01, float(speed_mps) * sync * sync / step)

# Дальность прогулки: от пары шагов до перехода через площадь.
NEAR_WALK = (2.0, 12.0)
FAR_WALK = (12.0, 55.0)

# Сколько стоять на месте между действиями.
SHORT_IDLE = (4.0, 25.0)
LONG_IDLE = (60.0, 480.0)

# Пауза «отошёл от клавиатуры»: бот в игре, но не двигается.
AWAY_CHANCE = 0.14
AWAY_TIME = (240.0, 900.0)

# Перерыв в игре: человек спит и не сидит в игре круглосуточно.
SESSION_HOURS = (3.0, 9.0)
BREAK_MINUTES = (12.0, 70.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _norm_angle(angle: float) -> float:
    return angle % 360.0


def _shortest_turn(current: float, target: float) -> float:
    """Кратчайший поворот по кругу: 350° → 10° это +20°, а не -340°."""
    diff = (target - current + 180.0) % 360.0 - 180.0
    return diff


class Humanizer:
    """Живое поведение: прогулки, осмотр, простой, перерывы.

    Работает через те же вызовы, что и раньше: MoveToCoord, SetKey,
    setbotrotation. Ничего нового на сервер не шлёт — только перестаёт
    делать однообразно.
    """

    def __init__(self, bot, api, session, log=None, rng=None):
        self.bot = bot
        self.api = api
        self.session = session
        self.log = log or (lambda _m: None)
        self.rng = rng or random.Random()

        self.rotation = 0.0
        self.home = None          # точка, вокруг которой гуляем
        self.session_started = time.time()
        self.session_limit = self.rng.uniform(*SESSION_HOURS) * 3600.0
        self.break_until = 0.0
        self.last_activity = ""
        self._last_pos = None

    # --- состояние ---

    def _position(self):
        try:
            pos = self.api.getbotposition()
        except Exception:
            return None
        if not pos or len(pos) < 3:
            return None
        if pos[0] == 0.0 and pos[1] == 0.0:
            return None
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    def _remember_home(self, pos) -> None:
        if self.home is None and pos is not None:
            self.home = pos
            self.log(f"[ЖИВОЙ] домашняя точка: {pos[0]:.1f}, {pos[1]:.1f}")

    def _set_rotation(self, angle: float) -> None:
        """Повернуть бота. Раньше здесь был setbotangle — такого метода нет,
        вызов молча падал в except, и бот вообще никогда не поворачивался."""
        angle = _norm_angle(angle)
        self.rotation = angle
        setter = getattr(self.api, "setbotrotation", None)
        if setter is None:
            return
        try:
            setter(angle)
        except Exception:
            pass

    def turn_towards(self, target_angle: float, steps: int = None) -> None:
        """Плавный поворот — человек не щёлкает камерой на 180°."""
        delta = _shortest_turn(self.rotation, target_angle)
        steps = steps or self.rng.randint(3, 7)
        for i in range(1, steps + 1):
            self._set_rotation(self.rotation + delta / steps)
            time.sleep(self.rng.uniform(0.03, 0.09))

    # --- действия ---

    def _sync_interval(self) -> float:
        """Фактический интервал sync-пакетов: от него зависит скорость хода."""
        sync = getattr(self.session, "sync", None)
        if sync is not None:
            try:
                low = float(sync.rate_player_min)
                high = float(sync.rate_player_max)
                return max(0.05, (low + high) * 0.5)
            except Exception:
                pass
        return 0.8

    def walk_to(self, x: float, y: float, z: float, *, run: bool = False) -> bool:
        """Дойти до точки настоящей ходьбой — тем же контроллером, что и всё.

        Раньше здесь стоял coord_delay=0.03 «наугад». В ECO-режиме это
        давало 1.2 см/с: бот не шёл, а стоял. Теперь скорость задаётся в
        м/с и пересчитывается в coord_delay под текущий интервал sync.

        Контроллер сам ведёт разгон, торможение, поворот и инерцию —
        повторять это руками не нужно и вредно.
        """
        pos = self._position()
        if pos is None:
            return False
        dist = math.hypot(x - pos[0], y - pos[1])
        if dist < 0.6:
            return True

        speed = self.rng.uniform(*(JOG_SPEED if run else WALK_SPEED))
        sync = self._sync_interval()
        coord_delay = coord_delay_for_speed(speed, sync)

        # Направление задаёт сам контроллер: он плавно доворачивает модель
        # по курсу движения и обновляет lr/ud — так же, как это делает
        # настоящий клиент. Дёргать поворот снаружи не надо.
        try:
            self.api.MoveToCoord(
                x, y, z,
                mode="walk",
                coord_delay=coord_delay,
                randomization=(6, 16),
                movement_direction_control=(True, True, 12, 100),
                movement_keys_mode="throttle:8" if run else "off",
                stop_on_dialog=True,
                completion_distance=0.8,
            )
        except Exception as exc:
            self.log(f"[ЖИВОЙ] шаг не вышел: {exc}")
            return False

        # Ждём прихода. Контроллер сам сообщает об остановке диалогом
        # (stop_on_dialog) — тогда выходим досрочно.
        timeout = _clamp(dist / max(0.4, speed * 0.6), 1.0, 60.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not getattr(self.bot, "running", True):
                self._stop_walking()
                return False
            now_pos = self._position()
            if now_pos is not None and math.hypot(now_pos[0] - x, now_pos[1] - y) < 1.3:
                return True
            time.sleep(0.2)

        self._stop_walking()
        return False

    def _stop_walking(self) -> None:
        try:
            self.api.StopMoveToCoord(reason="humanizer_stop")
        except Exception:
            pass

    def look_around(self) -> None:
        """Осмотреться: несколько поворотов с паузами, будто вертишь мышью."""
        base = self.rotation
        for _ in range(self.rng.randint(2, 4)):
            offset = self.rng.uniform(-140.0, 140.0)
            self.turn_towards(_norm_angle(base + offset))
            time.sleep(self.rng.uniform(0.5, 2.2))

    def stand(self, seconds: float) -> None:
        """Просто стоять, изредка чуть поворачиваясь."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not getattr(self.bot, "running", True):
                return
            time.sleep(min(self.rng.uniform(2.0, 7.0), max(0.1, deadline - time.time())))
            if self.rng.random() < 0.35:
                self.turn_towards(_norm_angle(self.rotation + self.rng.uniform(-45.0, 45.0)))

    def press_key(self, key: int, hold: float = None) -> None:
        """Нажать клавишу и отпустить. Присесть, прыгнуть, ударить."""
        hold = hold if hold is not None else self.rng.uniform(0.15, 0.7)
        try:
            self.api.SetKey(keys=key)
            time.sleep(hold)
            self.api.SetKey(keys=0)
        except Exception:
            pass

    # --- выбор занятия ---

    def _pick_point(self, far: bool = False) -> tuple | None:
        """Куда пойти. Вокруг домашней точки, не куда попало."""
        home = self.home
        pos = self._position()
        if home is None or pos is None:
            return None
        low, high = FAR_WALK if far else NEAR_WALK
        dist = self.rng.uniform(low, high)
        angle = self.rng.uniform(0.0, 2.0 * math.pi)
        x = home[0] + dist * math.cos(angle)
        y = home[1] + dist * math.sin(angle)
        # Высоту не угадываем: берём текущую, сервер поправит по земле.
        return (x, y, pos[2])

    def step(self) -> float:
        """Одно действие. Возвращает, сколько секунд можно не дёргать бота.

        Вызывающий код спит это время — вместо прежнего «спать 20-540 сек».
        """
        pos = self._position()
        self._remember_home(pos)
        if pos is None:
            return 5.0

        # Пора на перерыв: человек не сидит в игре сутками.
        if time.time() - self.session_started > self.session_limit:
            minutes = self.rng.uniform(*BREAK_MINUTES)
            self.log(f"[ЖИВОЙ] перерыв {minutes:.0f} мин — как у живого игрока")
            return -minutes * 60.0

        # Отошёл от клавиатуры: стоит, но не двигается.
        if self.rng.random() < AWAY_CHANCE:
            away = self.rng.uniform(*AWAY_TIME)
            self.last_activity = "отошёл"
            self.stand(away)
            return self.rng.uniform(*SHORT_IDLE)

        roll = self.rng.random()
        if roll < 0.40:
            # Прогулка по делам.
            point = self._pick_point(far=self.rng.random() < 0.3)
            if point is not None:
                self.last_activity = "гуляет"
                ok = self.walk_to(*point)
                if ok and self.rng.random() < 0.5:
                    self.look_around()
                return self.rng.uniform(*SHORT_IDLE)
            return 10.0

        if roll < 0.60:
            # Осмотрелся и пошёл обратно.
            self.last_activity = "осматривается"
            self.look_around()
            if self.rng.random() < 0.6:
                back = self._pick_point()
                if back is not None:
                    self.walk_to(*back)
            return self.rng.uniform(*SHORT_IDLE)

        if roll < 0.75:
            # Микродействие: присел, прыгнул, ударил.
            self.last_activity = "жест"
            self.press_key(self.rng.choice([2, 32, 1, 4]))
            time.sleep(self.rng.uniform(0.3, 1.5))
            return self.rng.uniform(*SHORT_IDLE)

        if roll < 0.90:
            # Стоит и вертит головой — самый частый вид живого АФК.
            self.last_activity = "стоит"
            self.stand(self.rng.uniform(*SHORT_IDLE))
            return self.rng.uniform(3.0, 15.0)

        # Долгий простой: отошёл покурить.
        self.last_activity = "простой"
        idle = self.rng.uniform(*LONG_IDLE)
        self.stand(idle)
        return self.rng.uniform(*SHORT_IDLE)

    def new_session(self) -> None:
        """После перерыва — новый цикл, новые сроки."""
        self.session_started = time.time()
        self.session_limit = self.rng.uniform(*SESSION_HOURS) * 3600.0
        self.home = None
