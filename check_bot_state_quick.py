from bot import BotManager
m = BotManager()
b = m.bots.get(65)
if b:
    print(f"Bot #65 [{b.nickname}] status: {b.status}")
    if b.api:
        print("Position:", b.api.getbotposition())
    if b.session and hasattr(b.session, 'players'):
        streamed = b.session.players.get_streamed_players()
        real = [p for p in streamed.values() if not getattr(p, 'is_npc', False)]
        print(f"Players in stream: {len(real)}")
        for p in real[:3]:
            print(f"  Player: {getattr(p, 'name', 'N/A')} @ ({p.x}, {p.y}, {p.z})")
    logs = list(b.logs)
    print("Recent logs:")
    for l in logs[-5:]:
        print(" ", l)
