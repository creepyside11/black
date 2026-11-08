"""Боевой запуск сборщика предметов с земли.

    python3 run_ground_pickup.py --id 1                      # холостой ход
    python3 run_ground_pickup.py --id 1 --armed              # реально поднимать
    python3 run_ground_pickup.py --id 1 --armed --route _diag/route.json

Холостой ход — режим по умолчанию. В нём бот ходит и пишет в журнал, что
ОТПРАВИЛ БЫ, но пакета не шлёт. Так проверяется маршрут, не трогая аккаунт.

Что должно быть верно ДО запуска с --armed:

  1. Бот в игре, персонаж живой.
  2. Предметы лежат рядом — в режиме route это гарантирует маршрут,
     в режиме hunt бот сам ищет их по потоку пикапов.
  3. pickuptime: между нажатиями держим паузу, чтобы не спамить.

Что делает бот: подходит к точке и шлёт ОДИН пакет RPC 413 с телом 18.
Id предмета в пакете нет — сервер сам берёт то, что лежит под игроком.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.events import emitter
from logic.ground_pickup import GroundPickup, load_route

TICK = 0.25              # пауза между шагами автомата
WAIT_INGAME = 180.0      # сколько ждать входа в игру


def _travel(bot):
    """Ходьба обычным MoveToCoord. Возвращает функцию для стенда."""

    def go(b, target):
        x, y, z = (float(v) for v in target)
        b.api.MoveToCoord(
            x, y, z, mode="walk", completion_distance=1.0,
        )

    return go


def main() -> int:
    ap = argparse.ArgumentParser(description="сборщик предметов с земли")
    ap.add_argument("--id", type=int, required=True, help="номер бота из accounts.json")
    ap.add_argument("--armed", action="store_true",
                    help="РЕАЛЬНО отправлять пакеты (по умолчанию холостой ход)")
    ap.add_argument("--route", default=None,
                    help="json с маршрутом [[время, x, y, z], ...]")
    ap.add_argument("--mode", choices=("route", "hunt"), default=None,
                    help="route — по маршруту, hunt — искать предметы самому")
    ap.add_argument("--radius", type=float, default=None, help="радиус жатвы")
    ap.add_argument("--limit", type=int, default=0,
                    help="остановиться после N поднятых предметов (0 — без предела)")
    args = ap.parse_args()

    mode = args.mode or ("route" if args.route else "hunt")
    route = load_route(args.route) if args.route else []

    print(f"[*] Бот #{args.id}, режим {mode}, armed={args.armed}")
    if route:
        print(f"[*] Маршрут: {len(route)} точек из {args.route}")
    if not args.armed:
        print("[!] ХОЛОСТОЙ ХОД. Пакеты не отправляются. Для боя: --armed")

    from bot import BotManager

    manager = BotManager()
    manager.start_selected([args.id])

    bot = manager.bots.get(args.id)
    if bot is None:
        print(f"[!] Нет бота #{args.id} в accounts.json")
        return 2

    wait_until = time.time() + WAIT_INGAME
    while time.time() < wait_until:
        if "В игре" in str(getattr(bot, "status", "")):
            break
        time.sleep(1.0)
    else:
        print(f"[!] Бот не вошёл в игру за {WAIT_INGAME:.0f} с, статус: {bot.status}")
        manager.stop_all()
        return 3

    print(f"[+] В игре: {getattr(bot, 'nickname', '?')} @ {getattr(bot, 'server_name', '?')}")

    kwargs = dict(
        route=route,
        mode=mode,
        armed=bool(args.armed),
        travel=_travel(bot),
        log=lambda m: bot.add_log(f"[СБОР] {m}") or print(f"[СБОР] {m}"),
    )
    if args.radius is not None:
        kwargs["hunt_radius"] = args.radius
        kwargs["pickup_range"] = args.radius

    stand = GroundPickup(bot, **kwargs)

    # on() — декоратор и падает на связанном методе; subscribe умеет.
    emitter.subscribe("onReceiveJSON", stand.on_receive_json, sync=True)
    emitter.subscribe("onReceiveRPC", stand.on_receive_rpc, sync=True)

    stand.start()

    try:
        while True:
            text = stand.step()
            if text and "иду" not in text:
                print(f"  {text}")
            if stand.state in ("done", "off"):
                break
            if args.limit and stand.grants >= args.limit:
                stand.stop(f"набрано {args.limit}")
                break
            time.sleep(TICK)
    except KeyboardInterrupt:
        print("\n[*] Остановка по Ctrl-C")
        stand.stop("Ctrl-C")
    finally:
        print()
        print(stand.dump_journal())
        print()
        for k, v in stand.report().items():
            print(f"  {k}: {v}")
        print()
        print("[*] Бот продолжает работать. Остановить: Ctrl-C ещё раз или закрыть процесс.")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            pass
        manager.stop_all()

    return 0


if __name__ == "__main__":
    sys.exit(main())
