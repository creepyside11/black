import time
import math
import sys
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
            return f"📍 {name} (в {dist:.1f}м от спавна)"
    return f"Координаты: X={pos[0]:.1f}, Y={pos[1]:.1f}, Z={pos[2]:.1f}"

# 4 бота на VORONEZH
bot_ids = [12, 13, 15, 16]
bots = [m.bots.get(i) for i in bot_ids if m.bots.get(i)]

# Выставляем правильный порт 80
for b in bots:
    b.port = 80
    print(f"▶️ Запуск #{b.bot_id} [{b.nickname}]...")
    b.start()
    time.sleep(2.0)

print("\n[*] Ждем 12 секунд авторизации...")
time.sleep(12)

# Принудительно выводим каждого бота в реальный мир
for b in bots:
    if b.session and b.session.connected:
        try:
            # 1. Отправляем SPAWN_SELECT интерфейс (вокзал)
            b.session.send_json(InterfaceID.SPAWN_SELECT, {"t": 1, "s": 0})
            b.session.send_json(InterfaceID.SPAWN_SELECT, {"s": 0})
            time.sleep(0.3)
            # 2. Шлем RequestClass и Spawn RPC
            b.session.send_rpc_request_class(0)
            time.sleep(0.2)
            b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
            # 3. Запускаем OnFoot синхронизацию
            if b.session.sync:
                b.session.sync.start()
            # 4. Делаем прыжок и запрос игроков
            if b.api:
                b.api.SetKey(keys=32)
                time.sleep(0.15)
                b.api.SetKey(keys=0)
                b.api.sendscoresandpings()
        except Exception:
            pass

time.sleep(4)

print("\n" + "="*65)
print("📊 АКТУАЛЬНЫЕ СПАВНЫ И ОКРУЖЕНИЕ ВСЕХ 4 БОТОВ:")
print("="*65)

for b in bots:
    pos = b.api.getbotposition() if b.api else None
    streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, 'players')) else {}
    real_p = [p for p in streamed.values() if not getattr(p, 'is_npc', False)]
    loc = where(pos)
    print(f"👤 #{b.bot_id} [{b.nickname}]")
    print(f"   Место: {loc}")
    print(f"   Координаты: {pos}")
    print(f"   Игроков рядом в стриме: {len(real_p)}")
    print(f"   Статус: {b.status}")
    print("-" * 40)

print("="*65 + "\n")

# Держим их живыми в реальном мире
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
