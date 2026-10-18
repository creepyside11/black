"""Телепорт под картой: модель не видно, для сервера это обычный ход.

Зачем. Delta-телепорт по поверхности проходит на 8.5 м/с, и анти-чит его не
трогает — но на такой скорости модель проезжает вплотную мимо игроков, её
видно. Уводим маршрут под рельеф: вниз, по горизонтали, вверх. Скорость на
всех участках одна и та же, поэтому по спидчеку это ровно тот же ход, что и
по поверхности. Меняется только высота.

Схема (D — глубина, ниже любого рельефа):

    старт (x0, y0, z0)
        |   вниз, x/y не меняются
    (x0, y0, D)
        |   по горизонтали
    (x1, y1, D)
        |   вверх
    цель (x1, y1, z1)

Почему это не должно палиться: сервер в SA-MP не считает физику, позицию
диктует клиент. Единственное, что он может проверить, — расстояние между
соседними синками. Оно одинаковое на всех трёх участках. Риск, который
остаётся и который надо проверить на живом сервере: отдельная проверка
«игрок ниже карты». Поэтому тест идёт сначала коротким зондом, а потом
полным маршрутом.
"""

from __future__ import annotations

import math
import time

from .delta_teleport import (
    SAFE_INTERVAL,
    SAFE_SPEED,
    DeltaTeleport,
    DeltaTeleportError,
    step_for_speed,
)

# Замерено на NOVGOROD, Нижегородск 1 (грунт z=11.6):
#   -30 ок в обе стороны, -50 ок, -60 ок, -70 спуск ок / подъём сбрасывает.
# Значит сервер держит глубину где-то около -65. Берём -60 с запасом.
# Глубже не нужно: под землёй модель и так не видно, а запас важнее.
UNDERMAP_DEPTH = -60.0

# Порог, ниже которого лезть под карту невыгодно: спуск и подъём съедят
# больше времени, чем сам путь.
MIN_DISTANCE = 60.0

LEG_DOWN = "down"
LEG_ACROSS = "across"
LEG_UP = "up"


def _dist(a, b) -> float:
    return math.sqrt(
        (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
    )


def plan_legs(start, target, depth: float = UNDERMAP_DEPTH) -> list:
    """Разбить маршрут на участки. Возвращает [(имя, точка), ...]."""
    x0, y0, z0 = (float(v) for v in start)
    x1, y1, z1 = (float(v) for v in target)
    depth = float(depth)

    legs = []

    # Вниз. Если мы уже ниже заданной глубины — берём нашу высоту, чтобы
    # не всплывать вверх перед спуском.
    floor = min(depth, z0, z1)
    if z0 > floor + 0.5:
        legs.append((LEG_DOWN, (x0, y0, floor)))

    # По горизонтали на этой высоте.
    if abs(x1 - x0) > 0.5 or abs(y1 - y0) > 0.5:
        legs.append((LEG_ACROSS, (x1, y1, floor)))

    # Вверх в цель.
    if z1 > floor + 0.5:
        legs.append((LEG_UP, (x1, y1, z1)))

    return legs


def estimate(start, target, depth: float = UNDERMAP_DEPTH, speed: float = SAFE_SPEED) -> dict:
    """Сколько займёт маршрут. Без сети, чистая арифметика."""
    legs = plan_legs(start, target, depth)
    total = 0.0
    detail = []
    here = tuple(float(v) for v in start)
    for name, point in legs:
        d = _dist(here, point)
        total += d
        detail.append({"leg": name, "point": point, "distance": d})
        here = point
    direct = _dist(start, target)
    return {
        "legs": detail,
        "distance": total,
        "direct_distance": direct,
        "overhead": total - direct,
        "seconds": (total / speed) if speed > 0 else 0.0,
        "direct_seconds": (direct / speed) if speed > 0 else 0.0,
        "needs_undermap": direct >= MIN_DISTANCE,
    }


def _bot_player(bot):
    return bot.session.players.bot


def _sender(bot):
    api = getattr(bot, "api", None)
    if api is not None and hasattr(api, "_delta_sync_sender"):
        return api._delta_sync_sender
    return bot.session.sync.send_player_sync


def travel_undermap(
    bot,
    target,
    *,
    depth: float = UNDERMAP_DEPTH,
    speed: float = SAFE_SPEED,
    interval: float = SAFE_INTERVAL,
    velocity_mode: str = "consistent",
    settle_packets: int = 1,
    log=None,
    on_leg=None,
    abort_check=None,
    force: bool = False,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    """Провести бота под картой в точку target.

    force=True — лезть под карту даже на коротком маршруте.
    Возвращает отчёт: status, legs, distance, elapsed, position.
    """
    log = log or (lambda msg: None)
    player = _bot_player(bot)
    send_sync = _sender(bot)

    start = tuple(float(v) for v in player.get_position())
    target = tuple(float(v) for v in target)
    plan = estimate(start, target, depth, speed)

    report = {
        "status": "ok",
        "reason": None,
        "start": start,
        "target": target,
        "depth": float(depth),
        "legs": [],
        "distance": plan["distance"],
        "direct_distance": plan["direct_distance"],
        "elapsed": 0.0,
        "position": start,
    }

    if not plan["needs_undermap"] and not force:
        report["status"] = "too_short"
        report["reason"] = (
            f"{plan['direct_distance']:.0f}м меньше порога {MIN_DISTANCE:.0f}м"
        )
        log(f"[ПОД КАРТОЙ] пропуск: {report['reason']}")
        return report

    legs = plan_legs(start, target, depth)
    if not legs:
        report["status"] = "already_there"
        report["position"] = target
        return report

    step = step_for_speed(speed, interval)
    abort = abort_check or (lambda: not bot.session.connected)

    log(
        f"[ПОД КАРТОЙ] {start[0]:.0f},{start[1]:.0f} -> {target[0]:.0f},{target[1]:.0f} "
        f"| участков {len(legs)} | путь {plan['distance']:.0f}м "
        f"| ~{plan['seconds']:.0f}с | глубина {depth:.0f}"
    )

    sync_was_on = bool(getattr(bot.session.sync, "enabled", False))
    if sync_was_on:
        bot.session.sync.stop()

    try:
        for index, (name, point) in enumerate(legs, 1):
            if abort():
                report["status"] = "aborted"
                report["reason"] = "session_down"
                return report

            engine = DeltaTeleport(
                player,
                send_sync,
                step=step,
                interval=interval,
                velocity_mode=velocity_mode,
                jitter=0.0,
                settle_packets=settle_packets,
                abort_check=abort,
                sleep=sleep,
                clock=clock,
            )
            res = engine.run(point)
            leg = {
                "leg": name,
                "target": point,
                "status": res["status"],
                "reason": res.get("reason"),
                "distance": res["distance"],
                "elapsed": res["elapsed"],
                "position": res["final_position"],
            }
            report["legs"].append(leg)

            now = tuple(float(v) for v in player.get_position())
            log(
                f"    участок {index}/{len(legs)} {name}: {res['status']}"
                f"{'/' + str(res.get('reason')) if res.get('reason') else ''} -> "
                f"({now[0]:.1f},{now[1]:.1f},{now[2]:.1f})"
            )

            if res["status"] not in ("ok", "already_there"):
                report["status"] = "aborted"
                # Причину берём у движка: session_down и server_reset — разные
                # вещи, и наверху их надо различать.
                report["reason"] = res.get("reason") or f"{name}:{res['status']}"
                report["position"] = now
                return report

            if on_leg is not None:
                try:
                    on_leg(leg, index, len(legs))
                except Exception:
                    pass
    except DeltaTeleportError as exc:
        report["status"] = "error"
        report["reason"] = str(exc)
        return report
    finally:
        if sync_was_on and bot.session.connected:
            bot.session.sync.start()

    final = tuple(float(v) for v in player.get_position())
    report["position"] = final
    report["elapsed"] = sum(l["elapsed"] for l in report["legs"])
    report["miss"] = _dist(final, target)
    if report["miss"] > 2.0:
        report["status"] = "off_target"
        report["reason"] = f"недолёт {report['miss']:.1f}м"
    log(
        f"[ПОД КАРТОЙ] готово: {report['status']} | "
        f"({final[0]:.1f},{final[1]:.1f},{final[2]:.1f}) | "
        f"недолёт {report['miss']:.2f}м | {report['elapsed']:.0f}с"
    )
    return report
