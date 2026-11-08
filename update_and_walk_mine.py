import time
import math
import os
import sys

# Stop current four_bots background script to take control
if os.path.exists("four_bots.pid"):
    with open("four_bots.pid") as f:
        try:
            os.kill(int(f.read().strip()), 9)
        except Exception:
            pass

from bot import BotManager

m = BotManager()
target_ids = [12, 13, 14, 15]

# Координаты Шахты (Вход в шахту):
MINE_X = 2379.218
MINE_Y = 1677.226
MINE_Z = 15.284

# Список спавнов для определения, где каждый бот:
known_spawns = [
    ("Спавн Батырево", 1795.375, 2518.425, 14.538),
    ("Спавн Лыткарино", -2428.754, 203.862, 26.134),
    ("Спавн Южный", 2745.431, -2436.016, 21.813),
    ("Спавн Нижегородск 1", -2661.548, 2000.877, 9.563),
    ("Спавн Нижегородск 2", -2152.833, 1558.348, 9.884),
]

def find_nearest_spawn(x, y):
    best_name = "Неизвестный спавн"
    best_dist = 99999.0
    for name, sx, sy, sz in known_spawns:
        d = math.hypot(x - sx, y - sy)
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name, best_dist

active_bots = []
for bid in target_ids:
    b = m.bots.get(bid)
    if b:
        active_bots.append(b)
        b.start()
        time.sleep(2.0)

print("[*] Подключаем ботов и определяем спавны...")
for _ in range(20):
    time.sleep(1)
    online_count = sum(1 for b in active_bots if "В игре" in b.status or "заспавнен" in b.status)
    if online_count >= 3:
        break

time.sleep(2)

print("\n" + "="*65)
print("ЛОКАЦИЯ И СПАВН КАЖДОГО БОТА:")
for b in active_bots:
    pos = b.api.getbotposition() if b.api else None
    if pos and not (pos[0] == 0.0 and pos[1] == 0.0):
        spawn_name, dist = find_nearest_spawn(pos[0], pos[1])
        d_mine = math.hypot(MINE_X - pos[0], MINE_Y - pos[1])
        print(f"• Бот #{b.bot_id} [{b.nickname}]:")
        print(f"    - Точка: {spawn_name} (погрешность: {dist:.1f}м)")
        print(f"    - Координаты: ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")
        print(f"    - Дистанция пешком до шахты: {d_mine:.0f}м")
    else:
        print(f"• Бот #{b.bot_id} [{b.nickname}]: в процессе подключения / авторизации")
print("="*65)
sys.stdout.flush()

# Запускаем фоновое пешее движение всех ботов к шахте через MoveToCoord(mode='walk')
print("\n[*] Запускаем штатное пешее движение ботов к шахте...")
import threading

def walk_worker(bot_obj):
    try:
        cur = bot_obj.api.getbotposition()
        if not cur or (cur[0] == 0.0 and cur[1] == 0.0):
            return
        bot_obj.add_log(f"[МАРШРУТ] Выдвигаемся пешком к шахте...")
        # Бег с интерполяцией на штатной скорости (10-11 м/с)
        bot_obj.api.MoveToCoord(MINE_X, MINE_Y, MINE_Z, mode="walk", coord_delay=0.95, completion_distance=3.0)
        bot_obj.add_log(f"[МАРШРУТ] Прибыли на шахту!")
    except Exception as e:
        bot_obj.add_log(f"[МАРШРУТ] Ошибка движения: {e}")

threads = []
for b in active_bots:
    t = threading.Thread(target=walk_worker, args=(b,), daemon=True)
    t.start()
    threads.append(t)

# Цикл контроля прогресса
start_time = time.time()
while True:
    time.sleep(15)
    print("\n--- Прогресс движения к шахте ---")
    for b in active_bots:
        pos = b.api.getbotposition() if b.api else None
        if pos and not (pos[0] == 0.0 and pos[1] == 0.0):
            rem = math.hypot(MINE_X - pos[0], MINE_Y - pos[1])
            print(f"  * #{b.bot_id} [{b.nickname}]: остаток {rem:.0f}м | коорд: ({pos[0]:.1f}, {pos[1]:.1f})")
    sys.stdout.flush()
