import time
import math
import os
import sys

# Kill old walk_sha
if os.path.exists("walk_sha.pid"):
    try:
        with open("walk_sha.pid") as fp:
            os.kill(int(fp.read().strip()), 9)
    except Exception:
        pass

from bot import BotManager
from samp.packets import PacketBuilder

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

# Стартовая точка — реальная поверхность Спавна Нижегородск 1
SPAWN_X = -2657.85
SPAWN_Y = 2005.30
SPAWN_Z = 10.45

print("[*] Подключаем Sha_Pismenskij к VORONEZH...")
b.start()

for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b.status: break

time.sleep(2)

# Ставим персонажа на поверхность спавна Нижегородска
b.session.players.bot.x = SPAWN_X
b.session.players.bot.y = SPAWN_Y
b.session.players.bot.z = SPAWN_Z

total_dist = math.hypot(TARGET_X - SPAWN_X, TARGET_Y - SPAWN_Y)
print(f"[+] Начальная точка: Спавн Нижегородск 1 ({SPAWN_X}, {SPAWN_Y}, {SPAWN_Z})")
print(f"[*] Точное расстояние до пикапа Нижегородск 2: {total_dist:.1f} м.")
print(f"[*] Пошаговое смещение по ~1.5м каждую секунду (игрок не врезается в здания)...")
sys.stdout.flush()

step_size = 1.5
num_steps = int(total_dist / step_size)
dx = (TARGET_X - SPAWN_X) / num_steps
dy = (TARGET_Y - SPAWN_Y) / num_steps
dz = (TARGET_Z - SPAWN_Z) / num_steps

cur_x, cur_y, cur_z = SPAWN_X, SPAWN_Y, SPAWN_Z
start_time = time.time()

for s in range(1, num_steps + 1):
    cur_x += dx
    cur_y += dy
    cur_z += dz

    b.session.players.bot.x = cur_x
    b.session.players.bot.y = cur_y
    b.session.players.bot.z = cur_z

    b.api.SetKey(keys=8) # кнопка W вперед

    if s % 10 == 0 or s == num_steps:
        rem = math.hypot(TARGET_X - cur_x, TARGET_Y - cur_y)
        elapsed = int(time.time() - start_time)
        print(f"[{elapsed}s | шаг {s}/{num_steps}] Коорд: ({cur_x:.1f}, {cur_y:.1f}) | Осталось: {rem:.1f}м")
        sys.stdout.flush()

    time.sleep(1.0)

b.api.SetKey(keys=0)
print(f"\n[!!!] Sha_Pismenskij ДОШЕЛ до пикапа скутеров Нижегородск 2!")
b.session.players.bot.x = TARGET_X
b.session.players.bot.y = TARGET_Y
b.session.players.bot.z = TARGET_Z

b.api.SendPickup(2050)
time.sleep(0.3)
b.api.SetKey(keys=1) # ALT
time.sleep(0.3)
b.api.SetKey(keys=0)

# Подтверждаем диалог аренды
try:
    b.session._sender.send(
        PacketBuilder.rpc_build("dialog_response", 1, 1, 0, ""),
        rpc_name="dialog_response",
    )
    b.session._sender.send(
        PacketBuilder.rpc_build("dialog_response", 0, 1, 0, ""),
        rpc_name="dialog_response",
    )
except Exception:
    pass

# Держим бота онлайн на пикапе
while True:
    time.sleep(3)
    b.session.players.bot.x = TARGET_X
    b.session.players.bot.y = TARGET_Y
    b.session.players.bot.z = TARGET_Z
    b.api.SetKey(keys=2)
    time.sleep(0.1)
    b.api.SetKey(keys=0)
