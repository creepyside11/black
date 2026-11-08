import time
from bot import BotManager
from samp.packets import PacketBuilder

m = BotManager()
b = m.bots.get(13) # Tamaria_Vatutin

b.start()
for _ in range(15):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(3)
pos = b.api.getbotposition() if b.api else None
st = b.session.players.get_streamed_players() if b.session else {}

print(f"Tamaria pos: {pos}")
print(f"Tamaria streamed players: {len(st)}")
for pid, p in list(st.items()):
    print(f"  -> {getattr(p, 'name', 'unnamed')} [ID {pid}] at ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

b.stop()
