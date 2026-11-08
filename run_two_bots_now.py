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

def identify_spawn(pos):
    if not pos or len(pos) < 3:
        return "Неизвестно"
    for name, s_pos in SPAWNS.items():
        dist = math.hypot(pos[0] - s_pos[0], pos[1] - s_pos[1])
        if dist < 120.0:
            return f"{name} ({dist:.1f}м)"
    return f"Координаты: {pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}"

b12 = m.bots.get(12) # Sha_Pismenskij
b13 = m.bots.get(13) # Tamaria_Vatutin

print("[*] Запускаем Sha_Pismenskij (#12)...")
b12.start()
time.sleep(3)

print("[*] Запускаем Tamaria_Vatutin (#13)...")
b13.start()

print("[*] Ждем авторизации и загрузки мира...")
for _ in range(15):
    time.sleep(1)

print("\n" + "="*50)
pos12 = b12.api.getbotposition() if b12.api else (0, 0, 0)
pos13 = b13.api.getbotposition() if b13.api else (0, 0, 0)

loc12 = identify_spawn(pos12)
loc13 = identify_spawn(pos13)

print(f"🤖 Бот #12 [{b12.nickname}]: {loc12} | Статус: {b12.status}")
print(f"🤖 Бот #13 [{b13.nickname}]: {loc13} | Статус: {b13.status}")
print("="*50 + "\n")

# Держим обоих онлайн
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
