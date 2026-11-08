import time
import math
from bot import BotManager

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij

print(f"[*] Подключаем бота {b.nickname}...")
b.start()

# Ждем авторизации
for i in range(15):
    time.sleep(1)
    if "авторизован" in b.status:
        break

time.sleep(1)

# Точка спавна Нижегородск 2
START_X = -2152.833
START_Y = 1558.348
START_Z = 9.884

# Выставляем стартовую позицию и синхронизируем
bot_player = b.session.players.bot
bot_player.update_position(START_X, START_Y, START_Z)

from samp.packets import PacketBuilder
b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
b.session.sync.start()
b.session.sync.send_player_sync()
b.api.sendscoresandpings()

print(f"\n[+] Бот заспавнен в Нижегородск 2! Координаты: {b.api.getbotposition()}")

# Цель: пикап скутера (-2169.809, 1580.214, 9.884)
TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

print(f"[*] Начинаем человеческий шаг к пикапу скутера...")
cur_x = START_X
cur_y = START_Y
cur_z = START_Z

step_dist = 1.2
while True:
    dx = TARGET_X - cur_x
    dy = TARGET_Y - cur_y
    dist = math.hypot(dx, dy)
    if dist < step_dist:
        cur_x, cur_y = TARGET_X, TARGET_Y
        bot_player.update_position(cur_x, cur_y, cur_z)
        b.session.sync.send_player_sync()
        break
    angle = math.atan2(dy, dx)
    cur_x += step_dist * math.cos(angle)
    cur_y += step_dist * math.sin(angle)
    bot_player.update_position(cur_x, cur_y, cur_z)
    b.session.sync.send_player_sync()
    time.sleep(0.8)

print(f"\n🎯 БОТ ДОШЕЛ ДО ПИКАПА СКУТЕРА! Координаты: {b.api.getbotposition()}")
streamed = b.session.players.get_streamed_players() if hasattr(b.session, 'players') else {}
real_players = [p for p in streamed.values() if not getattr(p, 'is_npc', False)]
print(f"👥 Реальных игроков рядом: {len(real_players)}")
for p in real_players[:5]:
    print(f"   * Игрок {getattr(p, 'name', 'unnamed')} @ ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

print("\n[*] Бот удерживает позицию у скутера и ожидает команду...")
try:
    while True:
        b.session.sync.send_player_sync()
        time.sleep(1.0)
except KeyboardInterrupt:
    b.stop()
