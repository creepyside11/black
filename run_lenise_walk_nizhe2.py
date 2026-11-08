import time
import math
import os
import sys

# Kill old script instances
for f in ["clean_walk.pid", "walk_scooter.pid", "loop_scooter.pid"]:
    if os.path.exists(f):
        try:
            with open(f) as fp:
                os.kill(int(fp.read().strip()), 9)
        except Exception:
            pass

from bot import BotManager

m = BotManager()
b = m.bots.get(15) # Lenise_Kustodiev @ Спавн Лыткарино

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

print(f"[*] Подключаем Lenise_Kustodiev (Спавн Лыткарино -> Нижегородск 2)...")
b.start()

for _ in range(20):
    time.sleep(1)
    if "В игре" in b.status or "заспавнен" in b.status:
        break

time.sleep(2)
start_pos = b.api.getbotposition() if b.api else None
print(f"[+] Бот в сети! Начальная точка (Спавн Лыткарино): {start_pos}")

cur_x, cur_y, cur_z = start_pos[0], start_pos[1], start_pos[2]
total_dist = math.hypot(TARGET_X - cur_x, TARGET_Y - cur_y)
print(f"[*] Общее расстояние до Нижегородск 2: {total_dist:.1f} м.")
sys.stdout.flush()

# Запускаем штатное пешее перемещение
# coord_delay=0.45 дает скорость ~15-18 км/ч (быстрый спринт), полностью легитимно для сервера
b.api.MoveToCoord(
    TARGET_X,
    TARGET_Y,
    TARGET_Z,
    mode="run",
    coord_delay=0.45,
    completion_distance=1.5
)

# Цикл мониторинга
start_time = time.time()
while True:
    time.sleep(3)
    pos = b.api.getbotposition() if b.api else None
    if not pos:
        print("[-] Связь с ботом потеряна...")
        break
    
    dist_left = math.hypot(TARGET_X - pos[0], TARGET_Y - pos[1])
    elapsed = int(time.time() - start_time)
    print(f"[+] [{elapsed}s] Позиция: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}) | Осталось: {dist_left:.1f}м | Статус: {b.status}")
    sys.stdout.flush()

    if dist_left <= 2.0:
        print(f"\n[!!!] Lenise_Kustodiev успешно ПРИБЫЛА на пикап скутеров Нижегородск 2!")
        # Активируем пикап скутеров
        b.api.SendPickup(2050)
        time.sleep(0.3)
        b.api.SetKey(keys=1) # ALT
        time.sleep(0.3)
        b.api.SetKey(keys=0)
        break

# Удерживаем бота на месте назначения
while True:
    time.sleep(3)
    if b.api:
        b.api.SetKey(keys=2)
        time.sleep(0.1)
        b.api.SetKey(keys=0)
