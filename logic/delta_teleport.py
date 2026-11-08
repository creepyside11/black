"""Delta-телепорт: перемещение пачкой коротких приращений вместо одного прыжка.

Сервер считает скорость как distance / dt между соседними sync-пакетами.
Один пакет с прыжком на километр — это мгновенный телепорт: скорость в тысячи
метров в секунду, античит такое режет (rubberband / kick).

Delta-метод рубит путь на равные шаги. Каждый шаг уходит отдельным OnFootSync
(или VehicleSync), а поле velocity в пакете считается из этого же шага, чтобы
позиция и скорость в пакете не противоречили друг другу. Сервер видит не
телепорт, а быстрое, но монотонное движение.
"""

from __future__ import annotations

import math
import random
import time

TICK_RATE = 50.0
TICK = 1.0 / TICK_RATE

# Скорость, которую сервер ещё считает честной.
#
# 8.0 м/с — решение владельца. Доказательства «6.0 спасает» у меня нет:
# запись живого клиента (_diag/route2.pcap) показывает, что сервер спокойно
# терпит 3.3 м/с на дистанции и velocity 0.10-0.12 на тик, но НЕ показывает,
# что он реагирует именно на скорость. Обрыв сессии происходит на старте
# перелёта, когда бот ещё не сдвинулся, — значит скорость там ни при чём.
# Что действительно видно в записи и чего не было у нас — форма трафика:
# рваный ритм, нулевые оси ввода и один sync на датаграм. Это и правим.
SAFE_SPEED = 8.0

# Человеческий ритм. Живой клиент не шлёт sync ровной очередью: в движении
# интервалы гуляют от 0.02 до 0.06 с, вместе с ними и velocity. Ровные
# 50 пакетов в секунду с постоянным шагом — подпись скрипта, а не игрока.
#
# Пауз внутри перелёта здесь нет намеренно. В записи живого клиента редкий
# sync (раз в полсекунды) — это когда он СТОИТ; в движении он шлёт часто.
# «Пойти на паузу, но продолжить сдвигать позицию» — это шаг на несколько
# метров одной датаграмой без пакетов между, то есть ровно тот самый
# телепорт-паттерн, от которого уходим. Стоячий режим с редким sync —
# задача шедулера (bot.py, sync_rate).
HUMAN_TICK_MIN = 0.022
HUMAN_TICK_MAX = 0.055
HUMAN_SPEED_JITTER = 0.18
# Живой клиент всё время движения держит ud = -127 (0xFF81) и жмёт спринт
# (keys = 8). У нас в пакете стояли нули — «игрок» ехал без ввода.
HUMAN_UD = -127
HUMAN_LR_JITTER = 8
HUMAN_SPRINT_KEYS = 8
# Стоячий sync живой клиент шлёт примерно раз в полсекунды.
HUMAN_IDLE_INTERVAL = 0.45

# Один пакет на тик. Позиция и velocity в пакете должны сходиться:
# step == speed * interval, иначе сервер видит расхождение и режет.
SAFE_INTERVAL = 1.0 / TICK_RATE
SAFE_STEP = SAFE_SPEED * SAFE_INTERVAL

# Дефолты — сразу безопасные. Старые 30м/0.15с давали 200 м/с: сервер
# отвечал SetPlayerPos обратно на доверенную точку, и бот не двигался.
DEFAULT_STEP = SAFE_STEP
DEFAULT_INTERVAL = SAFE_INTERVAL
DEFAULT_LEGIT_SPEED = SAFE_SPEED

VELOCITY_MODES = ("consistent", "legit", "zero")

MIN_INTERVAL = 0.02
MAX_INTERVAL = 5.0


def step_for_speed(speed: float, interval: float = SAFE_INTERVAL) -> float:
    """Шаг в метрах, при котором скорость в пакете совпадёт с реальной."""
    interval = _clamp(interval, MIN_INTERVAL, MAX_INTERVAL)
    return max(0.0, float(speed)) * interval


class DeltaTeleportError(Exception):
    """Некорректные параметры или невозможность стартовать телепорт."""


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(float(minimum), min(float(maximum), float(value)))


def _heading_degrees(ux: float, uy: float) -> float:
    # В SA-MP/CRMP угол 0 — север (+Y), 90 — запад (-X).
    return math.degrees(math.atan2(-ux, uy)) % 360.0


def _distance(a, b) -> float:
    return math.sqrt(
        (float(a[0]) - float(b[0])) ** 2
        + (float(a[1]) - float(b[1])) ** 2
        + (float(a[2]) - float(b[2])) ** 2
    )


class DeltaTeleport:
    """Движок delta-телепорта. Работает поверх PlayerInfo, без сети внутри."""

    def __init__(
        self,
        player,
        send_sync,
        *,
        step: float = DEFAULT_STEP,
        interval: float = DEFAULT_INTERVAL,
        velocity_mode: str = "consistent",
        legit_speed: float = DEFAULT_LEGIT_SPEED,
        keys: int | None = None,
        face_movement: bool = True,
        jitter: float = 0.05,
        settle_packets: int = 3,
        abort_check=None,
        on_progress=None,
        human: bool = False,
        tick_min: float = HUMAN_TICK_MIN,
        tick_max: float = HUMAN_TICK_MAX,
        speed_jitter: float = HUMAN_SPEED_JITTER,
        control_axes: bool = True,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        step = float(step)
        interval = float(interval)

        if not math.isfinite(step) or step <= 0.0:
            raise DeltaTeleportError("step должен быть положительным числом")
        if not math.isfinite(interval) or interval <= 0.0:
            raise DeltaTeleportError("interval должен быть положительным числом")

        velocity_mode = str(velocity_mode or "consistent").lower()
        if velocity_mode not in VELOCITY_MODES:
            raise DeltaTeleportError(
                f"velocity_mode должен быть одним из {VELOCITY_MODES}"
            )

        self.player = player
        self.send_sync = send_sync
        self.step = step
        self.interval = _clamp(interval, MIN_INTERVAL, MAX_INTERVAL)
        self.velocity_mode = velocity_mode
        self.legit_speed = max(0.0, float(legit_speed))
        # Без явной битмаски в человеческом режиме жмём спринт — ровно так
        # делает живой клиент, когда бежит (keys = 8 в записи route2.pcap).
        if keys is None:
            keys = HUMAN_SPRINT_KEYS if human else 0
        self.keys = int(keys) & 0xFFFF
        self.face_movement = bool(face_movement)
        self.jitter = _clamp(jitter, 0.0, 0.5)
        self.settle_packets = max(0, int(settle_packets))
        self.abort_check = abort_check
        self.on_progress = on_progress
        self._sleep = sleep
        self._clock = clock

        # Человеческий режим: интервал и скорость гуляют от пакета к пакету.
        # Выключен по умолчанию — оффлайн-тесты проверяют ровную математику.
        self.human = bool(human)
        self.tick_min = _clamp(tick_min, MIN_INTERVAL, MAX_INTERVAL)
        self.tick_max = _clamp(max(tick_max, self.tick_min), self.tick_min, MAX_INTERVAL)
        self.speed_jitter = _clamp(speed_jitter, 0.0, 0.5)
        self.control_axes = bool(control_axes)
        # Средняя скорость: шаг и интервал задают её, дальше её только
        # размазываем по времени, чтобы позиция и velocity не разошлись.
        self.nominal_speed = self.step / self.interval
        self.settle_interval = (
            HUMAN_IDLE_INTERVAL if self.human else self.interval
        )

    # ---------------------------------------------------------------- ритм

    def _sample_advance(self) -> tuple[float, float]:
        """Возвращает (шаг в метрах, интервал в секундах) для одного пакета.

        В human-режиме интервал гуляет, а шаг считается из него: тогда
        velocity в пакете всегда равна реальной скорости, и по пакету видно
        не ровную линию, а человеческий разброс.
        """
        if not self.human:
            return self.step, self.interval

        dt = random.uniform(self.tick_min, self.tick_max)

        speed = self.nominal_speed
        if self.speed_jitter > 0.0:
            speed *= 1.0 + random.uniform(-self.speed_jitter, self.speed_jitter)

        return speed * dt, dt

    def _reset_axes(self) -> None:
        if not self.control_axes:
            return
        if hasattr(self.player, "set_control_axes"):
            self.player.set_control_axes(0, 0)

    def _set_moving_axes(self) -> None:
        if not self.control_axes:
            return
        if hasattr(self.player, "set_control_axes"):
            lr = random.randint(-HUMAN_LR_JITTER, HUMAN_LR_JITTER)
            self.player.set_control_axes(lr, HUMAN_UD)

    # ------------------------------------------------------------------ plan

    def plan(self, target) -> dict:
        start = self.player.get_position()
        distance = _distance(start, target)
        if self.human:
            # Пакеты идут по фактическому интервалу, поэтому и план считаем
            # по средней длине шага, а не по номинальному self.step.
            mean_tick = (self.tick_min + self.tick_max) / 2.0
            mean_step = max(0.01, self.nominal_speed * mean_tick)
            steps = int(math.ceil(distance / mean_step)) if distance > 0.0 else 0
            duration = (
                (distance / self.nominal_speed) if self.nominal_speed > 0.0 else 0.0
            ) + self.settle_packets * self.settle_interval
        else:
            steps = int(math.ceil(distance / self.step)) if distance > 0.0 else 0
            duration = steps * self.interval + self.settle_packets * self.interval
        return {
            "start": tuple(start),
            "target": tuple(float(v) for v in target),
            "distance": distance,
            "steps": steps,
            "interval": self.interval,
            "step": self.step,
            "estimated_seconds": duration,
            "estimated_speed": (distance / (steps * self.interval)) if steps else 0.0,
        }

    # ------------------------------------------------------------------ apply

    def _apply_velocity(
        self,
        ux: float,
        uy: float,
        uz: float,
        advance: float,
        interval: float | None = None,
    ) -> None:
        if self.velocity_mode == "zero":
            self.player.velocity_x = 0.0
            self.player.velocity_y = 0.0
            self.player.velocity_z = 0.0
            return

        # Скорость считаем по фактическому интервалу ЭТОГО пакета, а не по
        # номинальному: с человеческим ритмом они разные, и velocity должна
        # совпадать с шагом, иначе сервер видит расхождение.
        dt = self.interval if interval is None else float(interval)
        if dt <= 0.0:
            dt = self.interval

        speed = advance / dt
        if self.velocity_mode == "legit":
            speed = min(speed, self.legit_speed)

        # velocity в sync-пакете живёт в единицах на тик 1/50 c.
        scale = speed * TICK
        self.player.velocity_x = ux * scale
        self.player.velocity_y = uy * scale
        self.player.velocity_z = uz * scale

    def _send(self) -> None:
        self.send_sync()

    # -------------------------------------------------------------------- run

    def run(self, target) -> dict:
        target = (float(target[0]), float(target[1]), float(target[2]))
        start = tuple(float(v) for v in self.player.get_position())
        started_at = self._clock()

        dx = target[0] - start[0]
        dy = target[1] - start[1]
        dz = target[2] - start[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)

        result = {
            "status": "ok",
            "start": start,
            "target": target,
            "final_position": start,
            "distance": distance,
            "steps": 0,
            "packets": 0,
            "elapsed": 0.0,
            "avg_speed": 0.0,
            "step": self.step,
            "interval": self.interval,
            "velocity_mode": self.velocity_mode,
            "reason": None,
        }

        if distance < 0.5:
            result["status"] = "already_there"
            result["final_position"] = target
            self._settle()
            result["elapsed"] = max(0.0, self._clock() - started_at)
            return result

        ux, uy, uz = dx / distance, dy / distance, dz / distance

        if self.face_movement:
            self.player.set_rotation(_heading_degrees(ux, uy))

        remaining = distance
        travelled = 0.0
        expected = start
        next_at = self._clock()
        packets = 0

        while remaining > 0.01:
            if self.abort_check is not None and self.abort_check():
                result["status"] = "aborted"
                result["reason"] = "session_down"
                break

            # Сервер может вернуть позицию назад (SCR_SET_PLAYER_POS) —
            # это rubberband, дальше шагать бессмысленно.
            current = tuple(float(v) for v in self.player.get_position())
            drift = _distance(current, expected)
            if drift > max(3.0, self.step * 0.5):
                result["status"] = "aborted"
                result["reason"] = "server_reset"
                result["server_position"] = current
                break

            advance, dt = self._sample_advance()
            advance = min(advance, remaining)
            if self.jitter > 0.0:
                advance *= 1.0 + random.uniform(-self.jitter, self.jitter)
                advance = _clamp(advance, 0.01, remaining)

            # Пауза ДО отправки, а не после: сервер считает скорость по
            # своему интервалу между пакетами, и если поспать после, он
            # увидит шаг пакета с интервалом предыдущего — позиция и
            # velocity разъедутся ровно на разницу интервалов.
            next_at += dt
            wait = next_at - self._clock()
            if wait > 0.0:
                self._sleep(wait)
            else:
                next_at = self._clock()

            nx = expected[0] + ux * advance
            ny = expected[1] + uy * advance
            nz = expected[2] + uz * advance

            self.player._set_position_internal(
                nx, ny, nz, update_velocity=False, now=time.time()
            )
            self._apply_velocity(ux, uy, uz, advance, dt)
            self._set_moving_axes()
            if self.keys:
                self.player.set_keys(self.keys)

            self._send()
            packets += 1
            expected = (nx, ny, nz)
            travelled += advance
            remaining -= advance
            result["steps"] += 1

            if self.on_progress is not None:
                try:
                    self.on_progress(
                        {
                            "position": expected,
                            "travelled": travelled,
                            "remaining": remaining,
                            "distance": distance,
                            "progress": _clamp(travelled / distance, 0.0, 1.0),
                            "steps": result["steps"],
                        }
                    )
                except Exception:
                    pass

        result["packets"] = packets

        if self.keys:
            self.player.set_keys(0)
        self._reset_axes()

        if result["status"] == "ok":
            self.player._set_position_internal(*target, update_velocity=False)
            self._settle()
            result["final_position"] = target
        else:
            self.player.reset_velocity()
            result["final_position"] = tuple(self.player.get_position())

        elapsed = max(0.0, self._clock() - started_at)
        result["elapsed"] = elapsed
        result["avg_speed"] = (travelled / elapsed) if elapsed > 0.0 else 0.0
        return result

    def _settle(self) -> None:
        """Гасим velocity и доигрываем пару стоячих пакетов.

        Стоячий пакет живой клиент шлёт примерно раз в полсекунды, а не
        каждые 20 мс: три пакета подряд с нулевой velocity в одном
        датаграме — это тоже подпись скрипта.
        """
        self.player.reset_velocity()
        self._reset_axes()
        for _ in range(self.settle_packets):
            self._send()
            self._sleep(self.settle_interval)


def parse_spawn_table(spawns: dict) -> dict:
    """Нормализует таблицу спавнов {имя: (x, y, z)} в {lower_name: (x, y, z)}."""
    table = {}
    for name, coords in (spawns or {}).items():
        try:
            table[str(name).strip().lower()] = tuple(float(v) for v in coords)
        except (TypeError, ValueError):
            continue
    return table


# Известные спавны Black Russia (проверенные координаты).
SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813),
}


def find_spawn(query: str):
    """Ищет спавн по имени: точное совпадение, подстрока, потом номер."""
    q = str(query or "").strip().lower()
    if not q:
        return None, None

    table = parse_spawn_table(SPAWNS)

    if q in table:
        return q, table[q]

    for name, coords in table.items():
        if q in name:
            return name, coords

    if q.isdigit():
        names = list(SPAWNS)
        index = int(q) - 1
        if 0 <= index < len(names):
            return names[index].lower(), table[names[index].lower()]

    return None, None
