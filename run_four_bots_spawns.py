import time
import math
import os
import sys
from bot import BotManager
from samp.interfaces import InterfaceID
from samp.packets import PacketBuilder

m = BotManager()

SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813)
}

def identify_spawn(pos):
    if not pos or len(pos) < 3 or (pos[0] == 0.0 and pos[1] == 0.0) or pos[2] < -5.0:
        return None
    for name, s_pos in SPAWNS.items():
        dist = math.hypot(pos[0] - s_pos[0], pos[1] - s_pos[1])
        if dist < 80.0:
            return name, dist
    return f"Карта ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})", 999.0

# 4 активных бота на VORONEZH
bot_ids = [12, 13, 15, 16] # Sha_Pismenskij, Tamaria_Vatutin, Lenise_Kustodiev, Juliette_Sadov

print(f"[*] Запускаем 4 ботов на VORONEZH: {bot_ids}...")
active_bots = []
for bid in bot_ids:
    b = m.bots.get(bid)
    if b and not b.is_banned:
        print(f"▶️ Запуск #{bid} [{b.nickname}]...")
        b.start()
        active_bots.append(b)
        time.sleep(3.0)

print("\n[*] Ждем 15 секунд полной авторизации и прогрузки мира...")
time.sleep(15)

# Для каждого бота отправляем подтверждение спавна в мир и прыжок
for b in active_bots:
    if b.session and b.session.connected:
        try:
            b.session.send_json(InterfaceID.SPAWN_SELECT, {"t": 1, "s": 0})
            b.session.send_json(InterfaceID.SPAWN_SELECT, {"s": 0})
            b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
            time.sleep(0.3)
            if b.api:
                b.api.SetKey(keys=32) # прыжок для прогрузки
                time.sleep(0.1)
                b.api.SetKey(keys=0)
        except Exception:
            pass

time.sleep(4)

print("\n" + "="*65)
print("📊 РЕАЛЬНЫЕ СПАВНЫ И ОКРУЖЕНИЕ БОТОВ:")
print("="*65)

nizhe2_found = []
other_spawns = []

for b in active_bots:
    pos = b.api.getbotposition() if b.api else None
    streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, 'players')) else {}
    real_players = [p for p in streamed.values() if not getattr(p, 'is_npc', False)]
    
    res = identify_spawn(pos)
    if res:
        name, dist = res
        status_line = f"Бот #{b.bot_id} [{b.nickname}] -> СПАВН: {name} | Игроков рядом: {len(real_players)} | Позиция: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})"
        print(status_line)
        if "Нижегородск 2" in name:
            nizhe2_found.append(b)
        else:
            other_spawns.append(b)
    else:
        print(f"Бот #{b.bot_id} [{b.nickname}] -> Ещё не появился на спавне (pos={pos})")
        other_spawns.append(b)

print("="*65 + "\n")

if nizhe2_found:
    print(f"🎯 ВНИМАНИЕ: На спавне Нижегородск 2 находится: {[b.nickname for b in nizhe2_found]}!")
    print("Оставляем их в игре и НЕ выходим!")
    # Отключаем остальных, если они не на Нижегородск 2
    for b in other_spawns:
        b.stop()
    # Держим ботов из Нижегородск 2
    while True:
        time.sleep(1)
else:
    print("[-] Ни один из 4 ботов не попал на Нижегородск 2.")
    print("[*] Отключаем всех и готовимся к перезаходу...")
    for b in active_bots:
        b.stop()
