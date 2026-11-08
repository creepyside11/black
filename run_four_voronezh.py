import time
import os
import sys
import math
from bot import BotManager
from logic.events import emitter

m = BotManager()

# Берем 4 аккаунта на сервере VORONEZH:
# #12: Sha_Pismenskij
# #13: Tamaria_Vatutin
# #14: Antonette_Vetchinkin
# #15: Lenise_Kustodiev
target_ids = [12, 13, 14, 15]

# Базовая точка сбора (Спавн Лыткарино / Батырево)
MEET_X = -2428.754
MEET_Y = 203.862
MEET_Z = 26.134

print(f"[*] Запускаем 4 ботов на VORONEZH: {target_ids}...")
active_bots = []
for bid in target_ids:
    b = m.bots.get(bid)
    if b:
        active_bots.append(b)
        print(f"[*] Запуск #{b.bot_id} [{b.nickname}]...")
        b.start()
        time.sleep(2.5)

print("\n[*] Ждем подключения и авторизации всех 4 ботов (25 сек)...")
for _ in range(25):
    time.sleep(1)
    online_count = sum(1 for b in active_bots if "В игре" in b.status or "заспавнен" in b.status)
    if online_count >= 3:
        break

print("\n[+] Синхронизируем всех ботов в одну точку (Спавн Лыткарино)...")
for idx, b in enumerate(active_bots):
    if b.session and hasattr(b.session, "players"):
        # Ставим каждого с микросмещением 1-2 метра, чтобы они стояли плотной группой
        offset_x = (idx % 2) * 1.5
        offset_y = (idx // 2) * 1.5
        b.session.players.bot.x = MEET_X + offset_x
        b.session.players.bot.y = MEET_Y + offset_y
        b.session.players.bot.z = MEET_Z
        if b.api:
            b.api.SetKey(keys=32)
            time.sleep(0.1)
            b.api.SetKey(keys=0)

time.sleep(2)

print("\n" + "="*60)
print(f"[ПРОВЕРКА ВЗАИМНОЙ ВИДИМОСТИ БОТОВ]")
for b in active_bots:
    pos = b.api.getbotposition() if b.api else None
    streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, "players")) else {}
    print(f"\nБот #{b.bot_id} [{b.nickname}] | Статус: {b.status} | Позиция: {pos}")
    print(f"  -> Видит игроков вокруг: {len(streamed)}")
    
    # Проверяем, видит ли этот бот других наших ботов:
    seen_our_bots = []
    other_names = {other.nickname: other.bot_id for other in active_bots if other.bot_id != b.bot_id}
    for pid, p in streamed.items():
        pname = getattr(p, "name", "")
        if pname in other_names:
            dist = math.hypot(p.x - pos[0], p.y - pos[1]) if pos else 0
            seen_our_bots.append((other_names[pname], pname, dist))
    
    if seen_our_bots:
        for ob_id, ob_name, d in seen_our_bots:
            print(f"  [+] ВИДИТ НАШЕГО БОТА #{ob_id} [{ob_name}] на расстоянии {d:.1f}м!")
    else:
        print("  [-] Пока не зафиксировал других ботов в стриме")
print("="*60)
sys.stdout.flush()

# Держим всех 4 ботов онлайн, прыгаем и сбрасываем АФК
print("\n[+] Все 4 бота удерживаются онлайн в точке сбора. Процесс активен...")
while True:
    time.sleep(5)
    for b in active_bots:
        if b.api and ("В игре" in b.status or "заспавнен" in b.status):
            b.api.SetKey(keys=2)
            time.sleep(0.1)
            b.api.SetKey(keys=0)
