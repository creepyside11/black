import time
from bot import BotManager
from samp.packets import PacketBuilder

m = BotManager()
b = m.bots.get(12)

# Check if bot is connected via controller or run directly
print("Bot connection status:", b.status if b else "None")
