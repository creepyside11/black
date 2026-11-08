import time
import math
from bot import BotManager

m = BotManager()

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

b12 = m.bots.get(12) # Sha_Pismenskij
b13 = m.bots.get(13) # Tamaria_Vatutin

print("[*] 1. Проверяем бота #12 [Sha_Pismenskij]...")
b12.start()
for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b12.status or "авторизован" in b12.status:
        break
time.sleep(2)
pos12 = b12.api.getbotposition() if b12.api else (0, 0, 0)
dist12 = math.hypot(TARGET_X - pos12[0], TARGET_Y - pos12[1]) if pos12[0] != 0 else 99999.0
print(f"  -> Sha_Pismenskij: pos={pos12} | dist={dist12:.1f}м | status={b12.status}")
b12.stop()
time.sleep(1)

print("\n[*] 2. Проверяем бота #13 [Tamaria_Vatutin]...")
b13.start()
for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b13.status or "авторизован" in b13.status:
        break
time.sleep(2)
pos13 = b13.api.getbotposition() if b13.api else (0, 0, 0)
dist13 = math.hypot(TARGET_X - pos13[0], TARGET_Y - pos13[1]) if pos13[0] != 0 else 99999.0
print(f"  -> Tamaria_Vatutin: pos={pos13} | dist={dist13:.1f}м | status={b13.status}")
b13.stop()

print("\n" + "="*50)
if dist12 < dist13:
    print(f"ВЫБРАН: Sha_Pismenskij (#12) - ближе ({dist12:.1f}м против {dist13:.1f}м)")
else:
    print(f"ВЫБРАН: Tamaria_Vatutin (#13) - ближе ({dist13:.1f}м против {dist12:.1f}м)")
print("="*50)
