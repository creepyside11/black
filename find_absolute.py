import time
from bot import BotManager

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

b.start()
for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b.status:
        break

time.sleep(2)
streamed = b.session.players.get_streamed_players()
print(f"Всего игроков в стрим-памяти: {len(streamed)}")

target = None
for pid, p in streamed.items():
    pname = getattr(p, "name", "")
    if "absolute" in pname.lower() or "capone" in pname.lower():
        target = (pid, pname, p.x, p.y, p.z)
        break

if target:
    print(f"\n[НАЙДЕН ИГРОК {target[1]}]: ID={target[0]} coords=({target[2]:.1f}, {target[3]:.1f}, {target[4]:.1f})")
    print(f"Идем прямо к нему вплотную...")
    b.session.players.bot.x = target[2]
    b.session.players.bot.y = target[3]
    b.session.players.bot.z = target[4]
    b.api.SetKey(keys=32)
    time.sleep(0.3)
    b.api.SetKey(keys=0)
    b.send_chat(f"{target[1]}, я стою прямо на тебе!")
else:
    print("Игрок Absolute_IzCapone в зоне видимости этого спавна не найден.")
    print("Список видимых игроков:")
    for pid, p in list(streamed.items())[:10]:
        print(f"  - {getattr(p, 'name', 'unnamed')} ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

b.stop()
