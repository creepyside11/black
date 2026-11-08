from bot import BotManager
m = BotManager()
b13 = m.bots.get(13)
b15 = m.bots.get(15)
print("B13:", b13.status if b13 else "None")
print("B15:", b15.status if b15 else "None")
