import time
import os
import sys
from bot import BotManager

m = BotManager()
b12 = m.bots.get(12) # Sha_Pismenskij
b13 = m.bots.get(13) # Tamaria_Vatutin

print("[*] Удерживаем Sha_Pismenskij и Tamaria_Vatutin онлайн на спавне Нижегородска...")
b12.start()
time.sleep(2)
b13.start()

while True:
    time.sleep(3)
    # Прыжок и сброс АФК для обоих
    for b in [b12, b13]:
        if b.api and ("В игре" in b.status or "заспавнен" in b.status):
            b.api.SetKey(keys=32)
            time.sleep(0.2)
            b.api.SetKey(keys=0)
            time.sleep(1.0)
            b.api.SetKey(keys=2)
            time.sleep(0.2)
            b.api.SetKey(keys=0)
