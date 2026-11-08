import time
import math
import os
import sys

from bot import BotManager
from samp.packets import PacketBuilder

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

print("[*] Подключаем Sha_Pismenskij к VORONEZH...")
b.start()

for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b.status:
        break

time.sleep(2)
start_pos = b.api.getbotposition() if b.api else None
print(f"[+] Бот заспавнен на вокзале Нижегородска: {start_pos}")

cur_x, cur_y, cur_z = start_pos[0], start_pos[1], start_pos[2]
if cur_x == 0 and cur_y == 0:
    cur_x, cur_y, cur_z = -2657.85, 2005.30, 10.45

total_dist = math.hypot(TARGET_X - cur_x, TARGET_Y - cur_y)
print(f"[*] Дистанция до пикапа скутеров Нижегородск 2: {total_dist:.1f}м")
print(f"[*] Начинаем шаги по ~1.2м каждую секунду (чистая скорость спокойной ходьбы)...")
sys.stdout.flush()

# Шаг 1.2 метра в секунду, чтобы персонаж гарантированно плавно проходил сквозь любые мелкие объекты
# и не упирался в коллизии зданий
step_size = 1.2
num_steps = int(total_dist / step_size)
dx = (TARGET_X - cur_x) / num_steps
dy = (TARGET_Y - cur_y) / num_steps
dz = (TARGET_Z - cur_z) / num_steps

start_time = time.time()

for s in range(1, num_steps + 1):
    cur_x += dx
    cur_y += dy
    cur_z += dz

    # Обновляем координаты персонажа
    b.session.players.bot.x = cur_x
    b.session.players.bot.y = cur_y
    b.session.players.bot.z = cur_z

    # Имитация шага/бега (клавиша вперед W = 8)
    b.api.SetKey(keys=8)

    # Каждые 10 шагов логируем оставшееся расстояние
    if s % 10 == 0 or s == num_steps:
        dist_now = math.hypot(TARGET_X - cur_x, TARGET_Y - cur_y)
        elapsed = int(time.time() - start_time)
        print(f"[{elapsed}s | шаг {s}/{num_steps}] Позиция: ({cur_x:.1f}, {cur_y:.1f}) | Осталось: {dist_now:.1f}м")
        sys.stdout.flush()

    time.sleep(1.0) # ровно 1 секунда на каждый шаг

b.api.SetKey(keys=0)
print(f"\n[!!!] Sha_Pismenskij ПРИБЫЛ на пикап скутеров ({TARGET_X}, {TARGET_Y}, {TARGET_Z})!")
sys.stdout.flush()

# Взаимодействие с пикапом скутеров
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

# Удерживаем бота онлайн на пикапе
while True:
    time.sleep(3)
    b.session.players.bot.x = TARGET_X
    b.session.players.bot.y = TARGET_Y
    b.session.players.bot.z = TARGET_Z
    b.api.SetKey(keys=2)
    time.sleep(0.1)
    b.api.SetKey(keys=0)
