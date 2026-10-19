"""Живой тест delta-телепорта: ОДИН бот, спавн -> ближайший ДРУГОЙ спавн.

    python3 test_delta_tp_spawn.py --bot Sha_Pismenskij
    python3 test_delta_tp_spawn.py --server VORONEZH --step 30 --interval 0.15

Сценарий:
  1. Поднимаем ровно один аккаунт.
  2. Ждём реальный спавн (не пре-спавн с z=-20).
  3. Определяем, на каком спавне бот стоит.
  4. Берём ближайший ДРУГОЙ спавн.
  5. Delta-телепорт туда.
  6. Наблюдаем: кик, дисконнект, откат позиции.
"""

import argparse
import sys
import time

from bot import BotManager
from logic.delta_teleport import DeltaTeleport, SPAWNS
from logic.events import emitter
from logic.spawn import is_spawned_position, nearest_spawn_name, station_name

# Аккаунты, которые исторически проходили авторизацию
KNOWN_GOOD = ("Sha_Pismenskij", "Moira_Amosov", "Carlos_Aretinskij")

SPAWN_WAIT = 180.0
WATCH_AFTER = 25.0

events = []


@emitter.on("onKick")
def _on_kick(bot_obj, reason_code, *args, **kwargs):
    events.append(("kick", reason_code))
    print(f"\n[!!!] КИК. Код причины: {reason_code}", flush=True)


@emitter.on("onDisconnect")
def _on_disconnect(bot_obj, *args, **kwargs):
    events.append(("disconnect", None))
    print("\n[!!!] ДИСКОННЕКТ", flush=True)


def is_spawned(bot):
    if not (bot.session and bot.session.connected and bot.api):
        return False
    return is_spawned_position(bot.api.getbotposition())


def nearest_spawn(pos, exclude=None):
    best, best_d = None, 1e18
    for name, c in SPAWNS.items():
        if exclude and name == exclude:
            continue
        d = ((pos[0] - c[0]) ** 2 + (pos[1] - c[1]) ** 2) ** 0.5
        if d < best_d:
            best, best_d = name, d
    return best, best_d


def pick_bot(manager, nick, server):
    if nick:
        for b in manager.bots.values():
            if b.nickname.lower() == nick.lower():
                return b
        return None

    if server:
        for b in manager.bots.values():
            if b.server_name.upper() == server and not b.is_banned:
                return b
        return None

    for b in manager.bots.values():
        if b.nickname in KNOWN_GOOD and not b.is_banned:
            return b
    return None


def wait_spawn(bot, timeout=SPAWN_WAIT):
    started = time.time()
    last_note = 0.0
    while time.time() - started < timeout:
        if bot.is_banned:
            print("[-] Бот забанен.")
            return False
        if is_spawned(bot):
            return True
        if time.time() - last_note > 10.0:
            last_note = time.time()
            pos = bot.api.getbotposition() if bot.api else None
            print(f"    ... {bot.status} | pos={pos} ({time.time()-started:.0f}с)", flush=True)
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", help="ник аккаунта")
    ap.add_argument("--server", help="сервер (первый незабаненный аккаунт)")
    ap.add_argument("--step", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--mode", default="consistent", choices=("consistent", "legit", "zero"))
    ap.add_argument("--watch", type=float, default=WATCH_AFTER)
    ap.add_argument("--host", help="переопределить хост сервера")
    ap.add_argument("--port", type=int, default=5125)
    args = ap.parse_args()

    manager = BotManager()
    bot = pick_bot(manager, args.bot, args.server)
    if bot is None:
        print("[-] Аккаунт не найден.")
        return 1

    if args.host:
        bot.host = args.host
        bot.port = args.port
        bot.server_name = f"{args.host}:{args.port}"

    print(f"[*] Один бот: #{bot.bot_id} [{bot.nickname}] | {bot.server_name} {bot.host}:{bot.port}")
    bot.set_task("stop")
    if not (bot.session and bot.session.connected):
        bot.start()

    print("[*] Ждём спавн...")
    if not wait_spawn(bot):
        print(f"[-] Спавн не получен. Статус: {bot.status}")
        bot.stop()
        return 1

    time.sleep(2.0)
    start = bot.api.getbotposition()
    here, here_d = nearest_spawn_name(start)
    print(f"\n[+] Спавн: ({start[0]:.1f}, {start[1]:.1f}, {start[2]:.1f})")
    if here:
        print(f"[+] Это «{here}» — {here_d:.0f}м от точки спавна")
    else:
        print(f"[+] Это «{station_name(start)}» — вокзала нет в таблице, берём как есть")

    streamed = bot.session.players.get_streamed_players()
    real = [p for p in streamed.values() if not getattr(p, "is_npc", False)]
    print(f"[+] Игроков в зоне стрима: {len(real)}")

    target_name, _ = nearest_spawn(start, exclude=here)
    target = SPAWNS[target_name]
    dist = ((start[0]-target[0])**2 + (start[1]-target[1])**2 + (start[2]-target[2])**2) ** 0.5
    print(f"[*] Ближайший ДРУГОЙ спавн: «{target_name}» ({target[0]:.1f}, {target[1]:.1f}, {target[2]:.1f})")
    print(f"[*] Дистанция: {dist:.0f}м")

    engine = DeltaTeleport(
        bot.session.players.bot,
        bot.api._delta_sync_sender,
        step=args.step,
        interval=args.interval,
        velocity_mode=args.mode,
        jitter=0.05,
        settle_packets=3,
        abort_check=lambda: not bot.session.connected,
        on_progress=lambda p: print(
            f"    шаг {p['steps']:3d} | {p['progress']*100:5.1f}% | осталось {p['remaining']:6.0f}м",
            flush=True,
        ) if p["steps"] % 5 == 0 else None,
    )

    plan = engine.plan(target)
    print(
        f"\n[*] План: {plan['steps']} шагов по {plan['step']:.0f}м, "
        f"интервал {plan['interval']:.2f}с, ~{plan['estimated_seconds']:.1f}с, "
        f"~{plan['estimated_speed']:.0f} м/с"
    )

    was_enabled = bool(getattr(bot.session.sync, "enabled", False))
    if was_enabled:
        bot.session.sync.stop()
    try:
        result = engine.run(target)
    finally:
        if was_enabled and bot.session.connected:
            bot.session.sync.start()

    print(f"\n[+] Результат: {result['status']}" + (f" ({result['reason']})" if result.get("reason") else ""))
    print(f"    шагов {result['steps']} | пакетов {result['packets']} | {result['elapsed']:.2f}с")
    print(f"    позиция: ({result['final_position'][0]:.1f}, {result['final_position'][1]:.1f}, {result['final_position'][2]:.1f})")

    print(f"\n[*] Наблюдаем {args.watch:.0f}с: кик, дисконнект, откат...")
    drift_max = 0.0
    t0 = time.time()
    while time.time() - t0 < args.watch:
        time.sleep(1.0)
        if not (bot.session and bot.session.connected):
            print("[-] Соединение потеряно.")
            break
        pos = bot.api.getbotposition()
        drift = ((pos[0]-target[0])**2 + (pos[1]-target[1])**2 + (pos[2]-target[2])**2) ** 0.5
        drift_max = max(drift_max, drift)
        print(
            f"    {time.time()-t0:4.0f}с | {bot.status} | "
            f"{pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f} | откат {drift:.1f}м",
            flush=True,
        )

    kicks = [e for e in events if e[0] == "kick"]
    drops = [e for e in events if e[0] == "disconnect"]
    connected = bool(bot.session and bot.session.connected)

    print("\n" + "=" * 60)
    print(f"Сервер: {bot.server_name} | бот: #{bot.bot_id} [{bot.nickname}]")
    print(f"Откуда: «{here}» -> Куда: «{target_name}» ({dist:.0f}м)")
    print(f"Режим: step={args.step} interval={args.interval} velocity={args.mode}")
    print(f"Кики: {len(kicks)} {[k[1] for k in kicks]}")
    print(f"Дисконнекты: {len(drops)}")
    print(f"Соединение: {'живо' if connected else 'потеряно'}")
    print(f"Статус: {bot.status}")
    print(f"Максимальный откат от цели: {drift_max:.1f}м")
    print(f"Итоговая позиция: {bot.api.getbotposition() if bot.api else None}")
    print("=" * 60)

    ok = not kicks and not drops and connected and drift_max < 30.0 and result["status"] == "ok"
    print("РЕЗУЛЬТАТ: " + ("ПРОШЁЛ — не кикнуло, позиция держится" if ok else "ПРОВАЛ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
