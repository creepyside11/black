"""Запуск бота шахты на одном аккаунте.

    python3 run_mine.py --nick Имя --armed            # в бой
    python3 run_mine.py --nick Имя                     # холостой ход
    python3 run_mine.py --nick Имя --armed --travel walk

Сценарий и все координаты — из записи `_diag/mine/mine.pcap`, разбор в
`logic/mine.py`. Здесь только обвязка: вход в игру, подписка на интерфейсы,
цикл шагов и уборка за собой.

Чего этот скрипт НЕ делает намеренно:

  * не трогает `accounts.json` (`freeze_accounts`), потому что сторож пула
    прокси и обработчик кика 999 переписывают файл через save_accounts();
  * не зовёт `manager.start_bot`/`stop_bot` — они поднимают весь пул;
  * не запускает капчу сам: решатель подключается отдельно, и без него бот
    на сдаче просто останавливается и говорит об этом.
"""

from __future__ import annotations

import argparse
import sys
import time

from logic.events import emitter
from logic.mine import (
    INTERIOR_Z,
    SPAWN_SETTLE,
    MINE_ENTRY,
    MINE_EXTERIOR,
    MineBot,
    S_FAILED,
    S_OFF,
    is_interior,
)
from logic.spawn import is_spawned_position
from run_collect import (
    find_bot,
    is_banned,
    log_to,
    make_travel,
    make_walk_travel,
    wait_in_game,
)

STEP_INTERVAL = 0.5          # как часто дёргаем автомат
STATUS_EVERY = 30.0          # как часто печатаем сводку


def wait_spawn(bot, log, timeout=180.0):
    """Дождаться НАСТОЯЩЕГО спавна, а не просто статуса «В игре».

    «В игре» наступает, когда персонаж уже в мире, но до выбора вокзала он
    висит в зоне преавторизации. Телепорт оттуда — это вылет или кик.
    Поэтому ждём позицию, которую is_spawned_position считает настоящей.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pos = bot.api.getbotposition()
        except Exception:
            pos = None
        if pos and is_spawned_position(pos):
            log(
                f"спавн: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}) — "
                f"персонаж в мире"
            )
            return pos
        time.sleep(1.0)
    return None


def wire_captcha(log):
    """Подключить решатель капчи из logic/captcha.py, если он готов.

    Без сети и без ключей решатель бесполезен, поэтому любой сбой здесь —
    не повод падать: бот просто останется без решателя.
    """
    try:
        from logic.captcha import ImageUrlSolver, SolverChain

        chain = SolverChain([ImageUrlSolver()])
        log("[*] Решатель капчи подключён")
        return chain
    except Exception as exc:
        log(f"[*] Решатель капчи недоступен: {exc!r}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Бот шахты Black Russia")
    ap.add_argument("--id", type=int, default=None, help="номер бота в accounts.json")
    ap.add_argument("--nick", default=None, help="ник бота")
    ap.add_argument("--proxy", default=None, help="прокси для этого прогона")
    ap.add_argument("--no-proxy", action="store_true", help="идти напрямую")
    ap.add_argument("--armed", action="store_true", help="БОЕВОЙ режим: пакеты уходят")
    ap.add_argument("--travel", choices=("teleport", "walk"), default="teleport",
                    help="как добираться ДО шахты: телепорт 8 м/с или пешком")
    ap.add_argument("--indoor", choices=("walk", "teleport"), default="walk",
                    help="как ходить ВНУТРИ шахты (по умолчанию пешком, как живой клиент)")
    ap.add_argument("--rounds", type=int, default=0,
                    help="кругов челнока, 0 — пока не надоест")
    ap.add_argument("--seconds", type=float, default=0,
                    help="остановиться через N секунд, 0 — без ограничения")
    ap.add_argument("--arrive", type=float, default=2.5, help="радиус прибытия, м")
    ap.add_argument("--settle", type=float, default=SPAWN_SETTLE,
                    help=f"сколько молчать после спавна, с (по умолчанию {SPAWN_SETTLE:.0f})")
    args = ap.parse_args()
    if args.id is None and not args.nick:
        ap.error("нужен --id или --nick")

    print("[*] Сценарий шахты:")
    print(f"    улица      {MINE_EXTERIOR}")
    print(f"    вход       {MINE_ENTRY}")
    print(f"    интерьер   всё, что выше z={INTERIOR_Z:.0f}")
    print(f"    до шахты   {'пешком' if args.travel == 'walk' else 'телепорт'}")
    print(f"    в шахте    {'пешком, 4.3 м/с' if args.indoor == 'walk' else 'ТЕЛЕПОРТ'}")
    if args.indoor == "teleport":
        print("    [!] Телепорт внутри шахты — плечи по 26 м. В записи там шаг.")
    print(f"    после спавна молчим {args.settle:.0f} с")
    print()
    if not args.armed:
        print("[!] ХОЛОСТОЙ ХОД. Пакеты не отправляются. Для боя: --armed")

    from bot import BotManager

    manager = BotManager()
    manager.freeze_accounts("прогон run_mine: реестр только на чтение")

    bot = find_bot(manager, args.id, args.nick)
    if bot is None:
        who = f"#{args.id}" if args.id is not None else repr(args.nick)
        print(f"[!] Нет бота {who} в accounts.json")
        return 2
    bot_id = bot.bot_id

    if args.proxy:
        bot.proxy = args.proxy
        bot.proxy_node = ""
        try:
            pool = getattr(manager, "proxy_pool", None)
            if pool is not None and pool.is_banned(bot.proxy):
                pool.unban(bot.proxy)
                print(f"[*] Метка «забанен» снята с {bot.proxy}")
        except Exception as exc:
            print(f"[*] Метку снять не вышло: {exc!r}")
    elif args.no_proxy:
        print(f"[*] Прокси {bot.proxy or '—'} отключён — идём напрямую")
        bot.proxy = ""
        bot.proxy_node = ""
    print(f"[*] Прокси: {bot.proxy or '— НЕТ, напрямую'}")

    log = log_to(bot, tag="ШАХТА")
    manager.start_selected([bot_id])

    if not wait_in_game(bot):
        print(f"[!] Бот не вошёл, статус: {bot.status}")
        manager.stop_all()
        return 3

    log(f"в игре: {getattr(bot, 'nickname', '?')} @ {getattr(bot, 'server_name', '?')}")

    spawn_pos = wait_spawn(bot, log)
    if spawn_pos is None:
        log("спавн не подтвердился — на землю так и не встали, выходим")
        try:
            manager.stop_all()
        except Exception:
            pass
        return 6
    spawn_at = time.time()

    # Снаружи и внутри — разные перевозчики. Внутри по умолчанию шаг:
    # в записи клиент проходит 26 метров за 39 секунд, а не прыжком.
    travel = make_walk_travel(log) if args.travel == "walk" else make_travel(log)
    travel_indoor = make_walk_travel(log) if args.indoor == "walk" else make_travel(log)
    game = MineBot(
        bot,
        travel=travel,
        travel_indoor=travel_indoor,
        log=log,
        armed=args.armed,
        arrive_radius=args.arrive,
        captcha_solver=wire_captcha(log),
        route_rounds=args.rounds,
        settle_seconds=args.settle,
    )
    # Отсчёт отстоя — от настоящего спавна, а не от запуска процесса.
    game.mark_spawn(spawn_at)
    emitter.subscribe("onReceiveJSON", game.on_receive_json, sync=True)
    game.start()

    started = time.time()
    last_status = 0.0
    code = 0
    try:
        while True:
            game.step()

            if is_banned(bot):
                log("аккаунт забанен — выходим")
                code = 4
                break
            if game.state == S_FAILED:
                log("сценарий застрял, выходим")
                code = 5
                break
            if game.state == S_OFF:
                log("автомат выключен, выходим")
                break

            now = time.time()
            if now - last_status >= STATUS_EVERY:
                last_status = now
                st = game.status()
                log(
                    f"состояние {st['состояние']} | диалог {st['страниц_диалога']} "
                    f"| ползунок {st['ползунков']} | плеч {st['плеч_маршрута']} "
                    f"| капч {st['капч_решено']}/{st['капч_видел']} "
                    f"| сдано {st['сдано']}"
                )

            if args.seconds and now - started >= args.seconds:
                log(f"отработали {args.seconds:.0f} с — останавливаемся")
                break

            time.sleep(STEP_INTERVAL)
    except KeyboardInterrupt:
        log("прервано с клавиатуры")
    finally:
        game.stop("конец прогона")
        try:
            print()
            print("[*] Журнал:")
            print(game.dump_journal(40))
            print()
            print("[*] Итог:", game.status())
        except Exception:
            pass
        try:
            manager.stop_all()
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
