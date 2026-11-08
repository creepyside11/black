import time
import math
from bot import BotManager

m = BotManager()

SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813)
}

def get_loc(pos):
    if not pos or len(pos) < 3 or (pos[0] == 0.0 and pos[1] == 0.0):
        return "Вход в игру / загрузка"
    for name, s_pos in SPAWNS.items():
        dist = math.hypot(pos[0] - s_pos[0], pos[1] - s_pos[1])
        if dist < 150.0:
            return f"📍 {name} (в {dist:.1f}м от спавна)"
    return f"Координаты: X={pos[0]:.1f}, Y={pos[1]:.1f}, Z={pos[2]:.1f}"

# Запускаем 4 ботов
b12 = m.bots.get(12) # Sha_Pismenskij
b13 = m.bots.get(13) # Tamaria_Vatutin
b15 = m.bots.get(15) # Lenise_Kustodiev
b16 = m.bots.get(16) # Juliette_Sadov

bots = [b12, b13, b15, b16]

for b in bots:
    if b:
        b.start()
        time.sleep(2.5)

print("\n[*] Ждем 20 секунд пока все 4 бота зайдут на сервер...")
time.sleep(20)

print("\n" + "="*60)
for b in bots:
    if not b:
        continue
    pos = b.api.getbotposition() if b.api else None
    streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, 'players')) else {}
    real_p = len([p for p in streamed.values() if not getattr(p, 'is_npc', False)])
    loc = get_loc(pos)
    print(f"👤 #{b.bot_id} [{b.nickname}]: {loc} | Игроков рядом: {real_p} | Статус: {b.status}")
print("="*60 + "\n")

# Держим всех в игре
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
