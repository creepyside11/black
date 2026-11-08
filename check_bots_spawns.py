import math
import time
from bot import BotManager

m = BotManager()
# The running bots are 12, 13, 14, 15
target_ids = [12, 13, 14, 15]

# Known spawn points provided by user:
known_spawns = [
    ("Спавн Батырево", 1795.375, 2518.425, 14.538),
    ("Спавн Лыткарино", -2428.754, 203.862, 26.134),
    ("Спавн Южный", 2745.431, -2436.016, 21.813),
    ("Спавн Нижегородск 1", -2661.548, 2000.877, 9.563),
    ("Спавн Нижегородск 2", -2152.833, 1558.348, 9.884),
]

# Read latest status from four_bots.log if running, or inspect
with open("four_bots.log", "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

print("--- ПОСЛЕДНИЕ СООБЩЕНИЯ ИЗ ЛОГА 4 БОТОВ ---")
for l in lines[-35:]:
    print(l.rstrip())
