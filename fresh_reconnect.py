import time
import os
import sys

# Kill any background bots first
for fname in ["bank_arzamas.pid", "controller.pid", "stay_online.pid", "respawn_lytkarino.pid", "live_bot.pid"]:
    if os.path.exists(fname):
        try:
            with open(fname) as f:
                pid = int(f.read().strip())
            os.kill(pid, 9)
        except Exception:
            pass

time.sleep(1)

from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

chat_lines = []

@emitter.on("onShowChat")
def on_chat(bot_obj, msg):
    chat_lines.append(msg)
    print(f"[ЧАТ] {msg}")
    sys.stdout.flush()

print("[*] Перезаходим на сервер VORONEZH под Sha_Pismenskij...")
b.start()

# Wait for full spawn
for _ in range(25):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(3)

pos = b.api.getbotposition() if b.api else None
print(f"\n[+] ТОЧНЫЕ КООРДИНАТЫ ПОСЛЕ ВХОДА: {pos}")

loc_name = m._get_bot_location_name(b)
print(f"[+] ЛОКАЦИЯ ПО БАЗЕ: {loc_name}")

streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, "players")) else {}
print(f"[+] ИГРОКОВ В ЗОНЕ ВИДИМОСТИ: {len(streamed)}")

for pid, p in list(streamed.items())[:15]:
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
    if pos:
        d = ((px - pos[0])**2 + (py - pos[1])**2 + (pz - pos[2])**2)**0.5
        print(f"    * {pname} [ID {pid}] | Дистанция: {d:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")
    else:
        print(f"    * {pname} [ID {pid}] ({px:.1f}, {py:.1f}, {pz:.1f})")

sys.stdout.flush()

# Keep alive loop
print("\n[+] Удерживаем бота в сети на точке спавна...")
while True:
    time.sleep(5)
    if b.api:
        b.api.SetKey(keys=2)
        time.sleep(0.3)
        b.api.SetKey(keys=0)
