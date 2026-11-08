import time
import math
from bot import BotManager
from samp.interfaces import InterfaceID
from samp.packets import PacketBuilder

m = BotManager()

SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813)
}

def where(pos):
    if not pos or len(pos) < 3 or (pos[0] == 0.0 and pos[1] == 0.0) or pos[2] < -5.0:
        return "В спект-камере (пре-спавн)"
    for name, s_pos in SPAWNS.items():
        dist = math.hypot(pos[0] - s_pos[0], pos[1] - s_pos[1])
        if dist < 120.0:
            return f"📍 {name} (в {dist:.1f}м)"
    return f"Координаты: X={pos[0]:.1f}, Y={pos[1]:.1f}, Z={pos[2]:.1f}"

b = m.bots.get(12) # Sha_Pismenskij
b.start()

print("[*] Ждем авторизации...")
for _ in range(15):
    time.sleep(1)
    if "авторизован" in b.status or "заспавнен" in b.status:
        break

print(f"Позиция до клика: {b.api.getbotposition()}")

# Шлем клик в диалог выбора спавна
if b.session and b.session.connected:
    # Black Russia спавн-меню отправляет json интерфейс SPAWN_SELECT
    b.session.send_json(InterfaceID.SPAWN_SELECT, {"s": 0})
    b.session.send_json(InterfaceID.SPAWN_SELECT, {"t": 1, "s": 0})
    b.session.send_json(InterfaceID.SPAWN_SELECT, {"t": 0, "s": 0})
    time.sleep(0.5)
    b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
    time.sleep(1.0)
    # Заставляем сделать шаг
    b.api.SetKey(keys=32)
    time.sleep(0.2)
    b.api.SetKey(keys=0)

time.sleep(2)
pos = b.api.getbotposition()
print(f"\nПОЗИЦИЯ ПОСЛЕ КЛИКА: {pos} -> {where(pos)}")
streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, 'players')) else {}
print(f"Игроков в зоне стрима: {len(streamed)}")
for pid, p in list(streamed.items())[:5]:
    print(f"  * Игрок {getattr(p, 'name', 'unnamed')} на coords ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

b.stop()
