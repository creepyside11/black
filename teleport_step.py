import time
import math
from bot import BotManager

m = BotManager()
b = m.bots.get(12)

# Stop old controller
import os
if os.path.exists("controller.pid"):
    with open("controller.pid") as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 9)
    except Exception:
        pass

b.start()
print("Connecting...")
for _ in range(20):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(2)
start_pos = b.api.getbotposition()
print(f"Spawn pos: {start_pos}")

# Bank of Arzamas:
target_x, target_y, target_z = 2280.0, -1450.0, 25.0

# Teleport in safe 40-meter steps so server sync accepts position without anticheat rubberband
cur_x, cur_y, cur_z = start_pos[0], start_pos[1], start_pos[2]
dist = math.hypot(target_x - cur_x, target_y - cur_y)
print(f"Distance to Arzamas Bank: {dist:.1f}m. Starting step sync...")

steps = int(dist // 35) + 1
for i in range(1, steps + 1):
    t = i / steps
    nx = cur_x + (target_x - cur_x) * t
    ny = cur_y + (target_y - cur_y) * t
    nz = cur_z + (target_z - cur_z) * t
    
    b.session.players.bot.x = nx
    b.session.players.bot.y = ny
    b.session.players.bot.z = nz
    b.api.SetKey(keys=0)
    time.sleep(0.15)
    if i % 10 == 0 or i == steps:
        print(f"Step {i}/{steps}: pos ({nx:.1f}, {ny:.1f}, {nz:.1f})")

time.sleep(1)
final_pos = b.api.getbotposition()
print(f"\n[+] ПРИБЫЛИ В БАНК АРЗАМАСА! Позиция: {final_pos}")

# Scan pickups at bank
pickups = b.session.stream.pickups if hasattr(b.session, "stream") else {}
print(f"[+] Пикапов вокруг банка: {len(pickups)}")
for pid, p in list(pickups.items())[:10]:
    d = math.hypot(p.x - final_pos[0], p.y - final_pos[1])
    if d < 50:
        print(f"    * Пикап #{pid} на расст {d:.1f}м: ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

streamed = b.session.players.get_streamed_players()
print(f"[+] Игроков вокруг банка Арзамаса: {len(streamed)}")
for pid, p in list(streamed.items())[:5]:
    print(f"    * {getattr(p, 'name', 'unnamed')} ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

# Keep online
print("[+] Бот стоит у Банка Арзамаса. Удерживаем онлайн...")
while True:
    time.sleep(5)
    b.api.SetKey(keys=2)
    time.sleep(0.3)
    b.api.SetKey(keys=0)
