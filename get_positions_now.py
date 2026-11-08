from bot import BotManager
import time
import math

m = BotManager()
b12 = m.bots.get(12)
b13 = m.bots.get(13)

SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813)
}

def where_is(pos):
    if not pos or len(pos) < 3 or (pos[0] == 0.0 and pos[1] == 0.0):
        return "В процессе авторизации / пре-спавн"
    for name, sp in SPAWNS.items():
        d = math.hypot(pos[0] - sp[0], pos[1] - sp[1])
        if d < 100.0:
            return f"{name} (расстояние {d:.1f}м)"
    return f"Координаты: {pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}"

pos12 = b12.api.getbotposition() if (b12 and b12.api) else None
pos13 = b13.api.getbotposition() if (b13 and b13.api) else None

print("Sha_Pismenskij:", where_is(pos12), "| статус:", b12.status if b12 else "none")
print("Tamaria_Vatutin:", where_is(pos13), "| статус:", b13.status if b13 else "none")
