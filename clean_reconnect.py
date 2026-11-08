import time
import sys
import re
from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

print(f"[*] Чистый вход бота #{b.bot_id} [{b.nickname}] на {b.server_name}...")

@emitter.on("onShowChat")
def on_chat(bot_obj, msg):
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', msg).strip()
    print(f"[ЧАТ] {clean}")
    sys.stdout.flush()

b.start()

# Ждем 20 секунд для полного прохождения авторизации и серверного спавна
for i in range(20):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(3)

# Координаты
pos = b.api.getbotposition() if b.api else None
print(f"\n[+] СТАТУС БОТА: {b.status}")
print(f"[+] КООРДИНАТЫ ПОСЛЕ ВХОДА: {pos}")

# Определяем город и место
loc_name = m._get_bot_location_name(b)
print(f"[+] ЛОКАЦИЯ: {loc_name}")

# Игроки рядом
streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, "players")) else {}
print(f"[+] ИГРОКОВ В ЗОНЕ СТРИМА: {len(streamed)}")

for pid, p in list(streamed.items())[:15]:
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
    if pos:
        d = ((px - pos[0])**2 + (py - pos[1])**2 + (pz - pos[2])**2)**0.5
        print(f"    * {pname} [ID {pid}] | Дистанция: {d:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")
    else:
        print(f"    * {pname} [ID {pid}] ({px:.1f}, {py:.1f}, {pz:.1f})")

sys.stdout.flush()

# Держим бота онлайн на точке спавна
print("\n[+] Бот стоит на месте онлайн. Не оффлайн.")
while True:
    time.sleep(5)
    if b.api:
        b.api.SetKey(keys=2)
        time.sleep(0.3)
        b.api.SetKey(keys=0)
