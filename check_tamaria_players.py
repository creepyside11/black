import time
from bot import BotManager

m = BotManager()
b = m.bots.get(13) # Tamaria_Vatutin @ VORONEZH

if not b:
    print("Bot 13 not found!")
    exit(1)

print(f"[*] Подключаем бота #{b.bot_id} [{b.nickname}] к VORONEZH...")
b.start()

for i in range(20):
    time.sleep(1)
    if "заспавнен" in b.status or "авторизован" in b.status:
        break

time.sleep(2)

pos = b.api.getbotposition() if b.api else None
print(f"[+] Позиция бота: {pos}")

streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, "players")) else {}
print(f"\n[+] ИГРОКОВ В ЗОНЕ ВИДИМОСТИ: {len(streamed)}")

for pid, p in list(streamed.items()):
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
    if pos:
        d = ((px - pos[0])**2 + (py - pos[1])**2 + (pz - pos[2])**2)**0.5
        print(f"  • {pname} [ID {pid}] | Дистанция: {d:.1f}м | Координаты: ({px:.1f}, {py:.1f}, {pz:.1f})")
    else:
        print(f"  • {pname} [ID {pid}] | Координаты: ({px:.1f}, {py:.1f}, {pz:.1f})")

# Держим бота онлайн на 20 секунд, сбрасываем АФК
print("\n[+] Удерживаем бота в сети...")
for _ in range(20):
    time.sleep(1)
    if b.api:
        b.api.SetKey(keys=2)
        time.sleep(0.2)
        b.api.SetKey(keys=0)

b.stop()
