import time
import sys
from bot import BotManager
from samp.interfaces import InterfaceID
from samp.packets import PacketBuilder

m = BotManager()
# Connect to bot #12 (we can run it with respawn logic)
b = m.bots.get(12)

# Stop the previous stay_online process first
import os
if os.path.exists("stay_online.pid"):
    try:
        with open("stay_online.pid") as f:
            pid = int(f.read().strip())
        os.kill(pid, 9)
        print(f"[*] Остановили предыдущий фоновый процесс PID {pid}")
    except Exception as e:
        print(f"[-] Ошибка остановки: {e}")

time.sleep(1)

# Check accounts to see if we can set spawn or select Lytkarino
print("[*] Запускаем бота с перехватом спавна...")

# Вокзал Лыткарино: индекс спавна в диалоге/интерфейсе Black Russia:
# Обычно 0 - Вокзал пгт. Батырево, 1 - Вокзал г. Южный, 2 - Вокзал г. Арзамас, 3 - Вокзал г. Лыткарино (или наоборот)
# Также на Black Russia есть команда /setspawn (доступна со 2 lvl или при выборе спавна)
# А без выхода с сервера спавн можно переключить через выбор класса / request_class / request_spawn

b.start()
for _ in range(20):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(2)
print(f"[+] Начальная позиция: {b.api.getbotposition()}")

# Попробуем без выхода с сервера вызвать смену спавна:
# 1. Отправляем select_spawn с кодом Лыткарино (в BR обычно 3 или 1)
print("[*] Пробуем отправить выбор спавна Лыткарино через InterfaceID.SPAWN_SELECT...")
# В Black Russia JSON interface SPAWN_SELECT: {'t': spawn_point_id}
# 0 - Батырево, 1 - Арзамас, 2 - Южный, 3 - Лыткарино
b.api.select_spawn(3)
time.sleep(0.5)

# 2. Запрашиваем респавн персонажа без дисконнекта:
b.session.send_rpc_request_class(3)
time.sleep(0.5)
b.session._sender.send(PacketBuilder.rpc_build("request_spawn"), rpc_name="request_spawn")
time.sleep(0.5)
b.session.send_rpc_spawn()
time.sleep(2)

pos_after = b.api.getbotposition()
print(f"[+] Позиция после запроса спавна: {pos_after}")

# 3. Если сервер не переместил серверным спавном, перемещаем координаты прямо в Лыткарино:
# Координаты вокзала Лыткарино (где пикап скутеров и остановка):
lytk_x, lytk_y, lytk_z = 2736.0, -2438.0, 22.3
print(f"[*] Отправляем координаты OnFoot на Вокзал Лыткарино ({lytk_x}, {lytk_y}, {lytk_z})...")
b.session.players.bot.x = lytk_x
b.session.players.bot.y = lytk_y
b.session.players.bot.z = lytk_z

# Прыгаем и кричим на вокзале Лыткарино
b.api.SetKey(keys=32)
time.sleep(0.3)
b.api.SetKey(keys=0)
time.sleep(1)

b.send_chat("Я на вокзале Лыткарино!")

# Сканируем игроков вокруг Лыткарино
streamed = b.session.players.get_streamed_players()
print(f"\n[+] Игроков в стрим-зоне в Лыткарино: {len(streamed)}")
for pid, p in list(streamed.items())[:15]:
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
    d = ((px - lytk_x)**2 + (py - lytk_y)**2 + (pz - lytk_z)**2)**0.5
    print(f"    * {pname} [ID {pid}] | Дистанция: {d:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")

print("\n[+] Бот зафиксирован на вокзале Лыткарино. Не оффлайн.")
sys.stdout.flush()

# Держим бота онлайн на вокзале Лыткарино, делаем прыжки каждые 5 сек
while True:
    time.sleep(5)
    b.api.SetKey(keys=32) # Прыжок
    time.sleep(0.3)
    b.api.SetKey(keys=0)
    time.sleep(5)
    b.api.SetKey(keys=2) # Приседание
    time.sleep(0.4)
    b.api.SetKey(keys=0)
    sys.stdout.flush()
