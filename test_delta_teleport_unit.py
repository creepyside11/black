"""Оффлайн-проверка delta-телепорта: без сети, на фейковом игроке.

Проверяем математику: разбиение пути, шаг, согласованность velocity с позицией,
темп пакетов, поведение при rubberband и при обрыве сессии.
"""

import math
import time

from logic.delta_teleport import (
    DeltaTeleport,
    DeltaTeleportError,
    find_spawn,
    SPAWNS,
)


class FakePlayer:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self._x, self._y, self._z = float(x), float(y), float(z)
        self.velocity_x = 0.0
        self.velocity_y = 0.0
        self.velocity_z = 0.0
        self.rotation = 0.0
        self.keys = 0

    def get_position(self):
        return (self._x, self._y, self._z)

    def _set_position_internal(self, x, y, z, *, update_velocity=True, now=None):
        self._x, self._y, self._z = float(x), float(y), float(z)

    def set_rotation(self, rotation):
        self.rotation = float(rotation)

    def set_keys(self, keys):
        self.keys = int(keys)

    def reset_velocity(self):
        self.velocity_x = self.velocity_y = self.velocity_z = 0.0


class Recorder:
    def __init__(self, player):
        self.player = player
        self.packets = []

    def __call__(self):
        self.packets.append(
            {
                "t": time.monotonic(),
                "pos": self.player.get_position(),
                "vel": (
                    self.player.velocity_x,
                    self.player.velocity_y,
                    self.player.velocity_z,
                ),
            }
        )


def check(name, condition, detail=""):
    status = "OK  " if condition else "FAIL"
    print(f"[{status}] {name}" + (f" | {detail}" if detail else ""))
    return bool(condition)


def run_case(name, start, target, step, interval, velocity_mode="consistent", jitter=0.0):
    print(f"\n=== {name} ===")
    player = FakePlayer(*start)
    rec = Recorder(player)
    engine = DeltaTeleport(
        player,
        rec,
        step=step,
        interval=interval,
        velocity_mode=velocity_mode,
        jitter=jitter,
        settle_packets=3,
        sleep=lambda _s: None,  # без реального ожидания
    )
    plan = engine.plan(target)
    result = engine.run(target)

    ok = True
    ok &= check("status == ok", result["status"] == "ok", result["status"])
    ok &= check(
        "финальная позиция совпала",
        all(abs(a - b) < 1e-6 for a, b in zip(result["final_position"], target)),
        str(result["final_position"]),
    )
    # С джиттером шаги чуть короче — может понадобиться пара лишних пакетов.
    steps_ok = (
        result["steps"] >= plan["steps"]
        if jitter > 0.0
        else result["steps"] == plan["steps"]
    )
    ok &= check(
        "число шагов == ceil(distance/step)",
        steps_ok,
        f"{result['steps']} vs {plan['steps']}",
    )
    ok &= check(
        "последний пакет в цели",
        all(abs(a - b) < 1e-3 for a, b in zip(rec.packets[-1]["pos"], target)),
        str(rec.packets[-1]["pos"]),
    )

    # Каждый шаг не длиннее step * (1 + jitter)
    max_jump = 0.0
    prev = start
    for p in rec.packets:
        max_jump = max(max_jump, math.dist(prev, p["pos"]))
        prev = p["pos"]
    limit = step * (1.0 + jitter) + 1e-3
    ok &= check(
        "нет прыжков больше шага",
        max_jump <= limit,
        f"max {max_jump:.2f} <= {limit:.2f}",
    )

    # velocity согласована с реальным приращением: |v| * 20 == пройденный путь
    worst = 0.0
    prev = start
    for p in rec.packets:
        moved = math.dist(prev, p["pos"])
        speed_from_pos = moved / interval
        speed_from_vel = math.sqrt(sum(v * v for v in p["vel"])) * 50.0
        if moved > 0.01:
            worst = max(worst, abs(speed_from_pos - speed_from_vel))
        prev = p["pos"]
    if velocity_mode == "consistent":
        ok &= check(
            "velocity в пакете совпадает с шагом",
            worst < 1.0,
            f"макс. расхождение {worst:.3f} м/с",
        )
    else:
        check("velocity mode", True, velocity_mode)

    print(
        f"       distance={result['distance']:.1f}м steps={result['steps']} "
        f"packets={result['packets']} план {plan['estimated_speed']:.1f} м/с"
    )
    return ok


def main():
    ok = True

    ok &= run_case("короткий прыжок 120м", (0, 0, 0), (120, 0, 0), 30, 0.15)
    ok &= run_case("средний 1.2км", (0, 0, 0), (900, 800, 0), 30, 0.15)
    ok &= run_case("дальний 5км", (0, 0, 0), (3000, 4000, 50), 45, 0.15)
    ok &= run_case("с джиттером", (0, 0, 0), (500, -500, 10), 30, 0.15, jitter=0.05)
    ok &= run_case("velocity=legit", (0, 0, 0), (600, 0, 0), 30, 0.15, velocity_mode="legit")
    ok &= run_case("velocity=zero", (0, 0, 0), (600, 0, 0), 30, 0.15, velocity_mode="zero")

    print("\n=== спавны ===")
    for query, expect in [("Лыткарино", "лыткарино"), ("ниже", "нижегородск 2"), ("2", "нижегородск 2")]:
        name, coords = find_spawn(query)
        ok &= check(f"find_spawn({query!r})", name == expect and coords is not None, f"{name} {coords}")

    print("\n=== ошибки параметров ===")
    for kwargs, label in [
        ({"step": 0}, "step=0"),
        ({"interval": -1}, "interval<0"),
        ({"velocity_mode": "warp"}, "velocity_mode=warp"),
    ]:
        try:
            DeltaTeleport(FakePlayer(), lambda: None, **kwargs)
            ok &= check(f"отклонён {label}", False)
        except DeltaTeleportError:
            ok &= check(f"отклонён {label}", True)

    print("\n=== rubberband ===")
    player = FakePlayer(0, 0, 0)
    packets = {"n": 0}

    def resetting_send():
        packets["n"] += 1
        if packets["n"] == 5:
            player._set_position_internal(0, 0, 0)  # сервер вернул назад

    engine = DeltaTeleport(
        player, resetting_send, step=30, interval=0.15, settle_packets=0,
        sleep=lambda _s: None,
    )
    res = engine.run((900, 0, 0))
    ok &= check("rubberband распознан", res["status"] == "aborted" and res["reason"] == "server_reset", str(res["status"]))

    print("\n=== обрыв сессии ===")
    player = FakePlayer(0, 0, 0)
    flag = {"down": False}

    def sending():
        if flag["down"]:
            return

    engine = DeltaTeleport(
        player, sending, step=30, interval=0.15, settle_packets=0,
        sleep=lambda _s: None, abort_check=lambda: flag["down"],
    )
    flag["down"] = True
    res = engine.run((900, 0, 0))
    ok &= check("обрыв распознан", res["status"] == "aborted" and res["reason"] == "session_down", str(res["status"]))

    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if ok else "ЕСТЬ ПАДЕНИЯ"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
