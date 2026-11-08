import time
from bot import BotManager

m = BotManager()
b = m.bots.get(16) # Juliette_Sadov @ VORONEZH

b.start()
for _ in range(15):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(3)
pos = b.api.getbotposition() if b.api else None
st = b.session.players.get_streamed_players() if b.session else {}

print(f"Juliette pos: {pos}")
print(f"Juliette streamed players: {len(st)}")
for pid, p in list(st.items())[:10]:
    print(f"  -> {getattr(p, 'name', 'unnamed')} [ID {pid}] at ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

b.stop()
