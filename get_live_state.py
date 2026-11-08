import time
import os
import sys
from bot import BotManager

m = BotManager()
b = m.bots.get(12)

# Start direct session and get players immediately
b.start()
for _ in range(25):
    time.sleep(1)
    if "В игре" in b.status or "заспавнен" in b.status:
        break

time.sleep(2)
pos = b.api.getbotposition() if b.api else None
print(f"POS: {pos}")
streamed = b.session.players.get_streamed_players() if b.session else {}
print(f"PLAYERS_COUNT: {len(streamed)}")
for pid, p in list(streamed.items())[:10]:
    print(f"PLAYER: {getattr(p, 'name', 'unnamed')} [ID {pid}] at ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

# Keep online and jump
print("Keeping online...")
while True:
    time.sleep(3)
    b.api.SetKey(keys=32)
    time.sleep(0.3)
    b.api.SetKey(keys=0)
