import time
import math
from bot import BotManager

m = BotManager()

# Запускаем двух заспавненных ботов на их РЕАЛЬНЫХ СПАВНАХ:
# Бот #13 (Tamaria_Vatutin) на Спавне Нижегородск 1 (-2664.0, 2005.9, 11.2)
# Бот #15 (Lenise_Kustodiev) на Спавне Южный (2740.5, -2441.0, 21.8)
b13 = m.bots.get(13)
b15 = m.bots.get(15)

print("[*] Подключаем ботов #13 и #15 на реальные спавны...")
b13.start()
time.sleep(2)
b15.start()

for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b13.status and "заспавнен" in b15.status:
        break

time.sleep(2)

print("\n" + "="*60)
print(f"• Бот #13 [{b13.nickname}]:")
print(f"    -> СПАВН: Спавн Нижегородск 1 (погрешность 5м)")
print(f"    -> Координаты: {b13.api.getbotposition()}")
st13 = b13.session.players.get_streamed_players() if b13.session else {}
print(f"    -> Игроков вокруг: {len(st13)}")
for pid, p in list(st13.items())[:5]:
    print(f"       * {getattr(p, 'name', 'unnamed')} [ID {pid}] at ({p.x:.1f}, {p.y:.1f})")

print(f"\n• Бот #15 [{b15.nickname}]:")
print(f"    -> СПАВН: Спавн Южный (погрешность 7м)")
print(f"    -> Координаты: {b15.api.getbotposition()}")
st15 = b15.session.players.get_streamed_players() if b15.session else {}
print(f"    -> Игроков вокруг: {len(st15)}")
for pid, p in list(st15.items())[:5]:
    print(f"       * {getattr(p, 'name', 'unnamed')} [ID {pid}] at ({p.x:.1f}, {p.y:.1f})")
print("="*60)

# Держим обоих онлайн на спавнах
print("\n[+] Боты #13 и #15 стоят на Спавне Нижегородск 1 и Спавне Южный. Удерживаем онлайн...")
while True:
    time.sleep(5)
    for b in [b13, b15]:
        if b.api and "заспавнен" in b.status:
            b.api.SetKey(keys=2)
            time.sleep(0.1)
            b.api.SetKey(keys=0)
