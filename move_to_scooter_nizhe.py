import time
import math
import os
import sys

# Stop old processes
for f in ["nizhe_live.pid", "walk_mine.pid", "spawns_real.pid"]:
    if os.path.exists(f):
        try:
            with open(f) as fp:
                os.kill(int(fp.read().strip()), 9)
        except Exception:
            pass

from bot import BotManager
from logic.events import emitter
from samp.packets import PacketBuilder

m = BotManager()
# Tamaria_Vatutin is bot #13
b = m.bots.get(13)

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

print(f"[*] Запуск бота Tamaria_Vatutin к точке пикапа скутеров ({TARGET_X}, {TARGET_Y}, {TARGET_Z})...")

@emitter.on("onShowDialog")
def dialog_handler(bot_obj, d_id, style, title, b1, b2, text):
    print(f"\n[ДИАЛОГ #{d_id}] {title}\n{text[:150]}")
    # Автоматически подтверждаем аренду скутера (кнопка 1 / Арендовать)
    time.sleep(0.3)
    try:
        b.session._sender.send(
            PacketBuilder.rpc_build("dialog_response", int(d_id), 1, 0, ""),
            rpc_name="dialog_response",
        )
        print(f"[+] Подтвердили диалог аренды #{d_id}!")
    except Exception as e:
        print(f"[-] Ошибка ответа на диалог: {e}")

b.start()
for _ in range(15):
    time.sleep(1)
    if "В игре" in b.status or "заспавнен" in b.status:
        break

time.sleep(2)
start_pos = b.api.getbotposition()
print(f"[+] Начальная позиция Tamaria: {start_pos}")

# Движение к целевой точке скутеров
print(f"[*] Начинаем движение к пикапу скутеров ({TARGET_X}, {TARGET_Y}, {TARGET_Z})...")
b.api.MoveToCoord(TARGET_X, TARGET_Y, TARGET_Z, mode="walk", coord_delay=0.1, completion_distance=1.0)
time.sleep(1.0)

cur_pos = b.api.getbotposition()
print(f"[+] Прибыли на точку скутеров: {cur_pos}")

# Осматриваем пикапы рядом с точкой
stream = b.session.stream if b.session else None
pickups = getattr(stream, "pickups", {}) if stream else {}
print(f"[+] Пикапов вокруг: {len(pickups)}")

nearest_p = None
min_dist = 999.0
for pid, p in list(pickups.items()):
    px, py, pz = float(getattr(p, 'x', 0)), float(getattr(p, 'y', 0)), float(getattr(p, 'z', 0))
    d = math.hypot(px - TARGET_X, py - TARGET_Y)
    if d < min_dist:
        min_dist = d
        nearest_p = (pid, px, py, pz)

if nearest_p and min_dist < 15.0:
    pid, px, py, pz = nearest_p
    print(f"[*] Наступаем на пикап аренды скутера #{pid} (расстояние {min_dist:.1f}м)...")
    b.api.MoveToCoord(px, py, pz, mode="walk", completion_distance=0.5)
    time.sleep(0.3)
    b.api.SendPickup(pid)
    b.api.SetKey(keys=1) # ALT
    time.sleep(0.2)
    b.api.SetKey(keys=0)
else:
    print("[*] Нажимаем клавишу ALT для аренды скутера на точке...")
    b.api.SetKey(keys=1)
    time.sleep(0.3)
    b.api.SetKey(keys=0)

time.sleep(2.0)

# Проверяем появление транспорта / скутера рядом
vehicles = getattr(stream, "vehicles", {}) if stream else {}
print(f"[+] Транспорта в зоне стрима: {len(vehicles)}")
nearest_veh = None
min_v_dist = 999.0
for vid, v in list(vehicles.items()):
    vx, vy, vz = float(getattr(v, 'x', 0)), float(getattr(v, 'y', 0)), float(getattr(v, 'z', 0))
    d = math.hypot(vx - TARGET_X, vy - TARGET_Y)
    if d < min_v_dist:
        min_v_dist = d
        nearest_veh = (vid, vx, vy, vz)

if nearest_veh and min_v_dist < 10.0:
    vid, vx, vy, vz = nearest_veh
    print(f"[*] Садимся на скутер #{vid} (дистанция {min_v_dist:.1f}м)...")
    b.api.put_in_vehicle(vid, seat_id=0)
    time.sleep(1.0)
    print(f"[+] Сели в транспорт! is_in_vehicle: {b.session.players.bot.is_in_vehicle()}")

# Держим бота онлайн на точке/скутере
print("\n[+] Tamaria_Vatutin онлайн на точке скутеров. Удерживаем процесс...")
while True:
    time.sleep(5)
    if b.api and not b.session.players.bot.is_in_vehicle():
        b.api.SetKey(keys=2)
        time.sleep(0.2)
        b.api.SetKey(keys=0)
