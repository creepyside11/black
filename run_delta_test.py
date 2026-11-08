"""Перебирает аккаунты, пока один не заспавнится, и гоняет delta-телепорт.

    python3 run_delta_test.py [--step 30] [--interval 0.15]

Одновременно поднят ровно ОДИН бот: предыдущий гасится перед следующим.
"""

import argparse
import sys
import time

from bot import BotManager
from logic.delta_teleport import DeltaTeleport, SPAWNS
from logic.events import emitter

# Порядок: сначала те, что уже проходили авторизацию
CANDIDATES = [
    "Sha_Pismenskij",     # VORONEZH
    "Moira_Amosov",       # TVER
    "Carlos_Aretinskij",  # VOLOGDA
    "Shuntae_Burmin",     # NORILSK
]

SPAWN_WAIT = 90.0
events = []


@emitter.on("onKick")
def _on_kick(bot_obj, reason_code, *args, **kwargs):
    events.append(("kick", reason_code))
    print(f"    [!!!] КИК {reason_code}", flush=True)


@emitter.on("onDisconnect")
def _on_disconnect(bot_obj, *args, **kwargs):
    events.append(("disconnect", None))
    print("    [!!!] ДИСКОННЕКТ", flush=True)


def is_spawned(bot):
    if not (bot.session and bot.session.connected and bot.api):
        return False
    pos = bot.api.getbotposition()
    if not pos or len(pos) < 3:
        return False
    if abs(pos[0]) < 0.5 and abs(pos[1]) < 0.5:
        return False
    return pos[2] > -5.0


def wait_spawn(bot, timeout=SPAWN_WAIT):
    t0 = time.time()
    last = 0.0
    while time.time() - t0 < timeout:
        if is_spawned(bot):
            return True
        if bot.is_banned:
            return False
        if time.time() - last > 15.0:
            last = time.time()
            pos = bot.api.getbotposition() if bot.api else None
            print(f"    ... {bot.status} pos={pos}", flush=True)
        time.sleep(0.5)
    return False


def nearest_spawn(pos, exclude=None):
    best, best_d = None, 1e18
    for name, c in SPAWNS.items():
        if exclude and name == exclude:
            continue
        d = ((pos[0] - c[0]) ** 2 + (pos[1] - c[1]) ** 2) ** 0.5
        if d < best_d:
            best, best_d = name, d
    return best, best_d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=30.0)
    ap.add_argument("--interval", type=float, default=0.15)
    ap.add_argument("--mode", default="consistent", choices=("consistent", "legit", "zero"))
    ap.add_argument("--watch", type=float, default=25.0)
    args = ap.parse_args()

    manager = BotManager()
    bot = None

    for nick in CANDIDATES:
        candidate = next((b for b in manager.bots.values() if b.nickname == nick), None)
        if candidate is None or candidate.is_banned:
            print(f"[-] {nick}: нет в accounts.json или забанен", flush=True)
            continue

        print(f"\n[*] Пробуем {nick} на {candidate.server_name}...", flush=True)
        events.clear()
        candidate.set_task("stop")
        candidate.start()

        if wait_spawn(candidate):
            bot = candidate
            break

        print(f"[-] {nick} не заспавнился ({candidate.status}) — гасим, пауза 15с", flush=True)
        candidate.stop()
        time.sleep(15.0)

    if bot is None:
        print("\n[-] Ни один аккаунт не заспавнился.")
        return 1

    time.sleep(2.0)
    start = bot.api.getbotposition()
    here, here_d = nearest_spawn(start)
    print(f"\n[+] Спавн: ({start[0]:.1f}, {start[1]:.1f}, {start[2]:.1f}) — «{here}» ({here_d:.0f}м)")
    streamed = bot.session.players.get_streamed_players()
    real = [p for p in streamed.values() if not getattr(p, "is_npc", False)]
    print(f"[+] Игроков в зоне стрима: {len(real)}")

    target_name, _ = nearest_spawn(start, exclude=here)
    target = SPAWNS[target_name]
    dist = ((start[0]-target[0])**2 + (start[1]-target[1])**2 + (start[2]-target[2])**2) ** 0.5
    print(f"[*] Ближайший ДРУГОЙ спавн: «{target_name}» — {dist:.0f}м")

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
    print(f"[*] План: {plan['steps']} шагов, ~{plan['estimated_seconds']:.1f}с, ~{plan['estimated_speed']:.0f} м/с")

    was_enabled = bool(getattr(bot.session.sync, "enabled", False))
    if was_enabled:
        bot.session.sync.stop()
    try:
        result = engine.run(target)
    finally:
        if was_enabled and bot.session.connected:
            bot.session.sync.start()

    print(f"\n[+] {result['status']} | шагов {result['steps']} | {result['elapsed']:.2f}с")
    print(f"    позиция: ({result['final_position'][0]:.1f}, {result['final_position'][1]:.1f}, {result['final_position'][2]:.1f})")

    print(f"\n[*] Наблюдаем {args.watch:.0f}с...")
    drift_max = 0.0
    t0 = time.time()
    while time.time() - t0 < args.watch:
        time.sleep(1.0)
        if not (bot.session and bot.session.connected):
            print("[-] соединение потеряно", flush=True)
            break
        pos = bot.api.getbotposition()
        drift = ((pos[0]-target[0])**2 + (pos[1]-target[1])**2 + (pos[2]-target[2])**2) ** 0.5
        drift_max = max(drift_max, drift)
        print(f"    {time.time()-t0:4.0f}с | {bot.status} | {pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f} | откат {drift:.1f}м", flush=True)

    kicks = [e for e in events if e[0] == "kick"]
    drops = [e for e in events if e[0] == "disconnect"]
    connected = bool(bot.session and bot.session.connected)

    print("\n" + "=" * 62)
    print(f"Сервер {bot.server_name} | #{bot.bot_id} [{bot.nickname}]")
    print(f"«{here}» -> «{target_name}» ({dist:.0f}м) | step={args.step} interval={args.interval} vel={args.mode}")
    print(f"Кики: {len(kicks)} {[k[1] for k in kicks]} | дисконнекты: {len(drops)}")
    print(f"Соединение: {'живо' if connected else 'потеряно'} | статус: {bot.status}")
    print(f"Макс. откат: {drift_max:.1f}м")
    print("=" * 62)
    ok = not kicks and not drops and connected and drift_max < 30.0 and result["status"] == "ok"
    print("РЕЗУЛЬТАТ: " + ("ПРОШЁЛ" if ok else "ПРОВАЛ"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
