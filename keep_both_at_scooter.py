import time
from bot import BotManager
from samp.packets import PacketBuilder

m = BotManager()
b12 = m.bots.get(12) # Sha_Pismenskij
b13 = m.bots.get(13) # Tamaria_Vatutin

TARGET_X = -2169.809
TARGET_Y = 1580.214
TARGET_Z = 9.884

print("[*] Ставим обоих ботов на пикап скутеров Нижегородск 2...")
b12.start()
time.sleep(2)
b13.start()

# Синхронизируем обоих на пикапе
for b in [b12, b13]:
    if b and b.session:
        b.session.players.bot.x = TARGET_X
        b.session.players.bot.y = TARGET_Y
        b.session.players.bot.z = TARGET_Z
        if b.api:
            # Нажимаем ALT на пикапе
            b.api.SetKey(keys=1)
            time.sleep(0.3)
            b.api.SetKey(keys=0)

# Подтверждаем диалог аренды скутера
for b in [b12, b13]:
    if b and b.session:
        try:
            b.session._sender.send(
                PacketBuilder.rpc_build("dialog_response", 1, 1, 0, ""),
                rpc_name="dialog_response",
            )
        except Exception:
            pass

print("\n[+] Оба бота зафиксированы на пикапе скутеров Нижегородск 2!")
print(f"  -> Координаты: ({TARGET_X}, {TARGET_Y}, {TARGET_Z})")

while True:
    time.sleep(3)
    for b in [b12, b13]:
        if b and b.api:
            b.api.SetKey(keys=2)
            time.sleep(0.1)
            b.api.SetKey(keys=0)
