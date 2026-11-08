import time
from bot import BotManager

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

b.start()
for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b.status:
        break

time.sleep(2)
print("Spawn position:", b.api.getbotposition())

# Вокзал Лыткарино точные координаты: 2736.0, -2438.0, 22.3
# Перемещаем бота на вокзал Лыткарино
lytk_x, lytk_y, lytk_z = 2736.0, -2438.0, 22.3
print(f"Moving to Lytkarino Station ({lytk_x}, {lytk_y}, {lytk_z})...")
b.session.players.bot.x = lytk_x
b.session.players.bot.y = lytk_y
b.session.players.bot.z = lytk_z

# Обновляем onfoot синхронизацию
b.api.SetKey(keys=32)
time.sleep(0.3)
b.api.SetKey(keys=0)
time.sleep(1)

# Сканируем игроков вокруг в Лыткарино!
streamed = b.session.players.get_streamed_players()
print(f"[+] Игроков в Лыткарино вокруг бота: {len(streamed)}")
for pid, p in list(streamed.items())[:15]:
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0), getattr(p, "y", 0), getattr(p, "z", 0)
    dist = ((px - lytk_x)**2 + (py - lytk_y)**2 + (pz - lytk_z)**2)**0.5
    print(f"    * {pname} [ID {pid}] на расст. {dist:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")

b.send_chat("Я тут на вокзале Лыткарино!")

# Держим бота 60 секунд, прыгаем каждые 3 секунды
print("[+] Бот стоит в Лыткарино 60 секунд...")
start = time.time()
while time.time() - start < 60:
    time.sleep(2)
    b.api.SetKey(keys=32) # Прыжок
    time.sleep(0.3)
    b.api.SetKey(keys=0)
    time.sleep(2)
    b.api.SetKey(keys=2) # Приседание
    time.sleep(0.4)
    b.api.SetKey(keys=0)

b.stop()
