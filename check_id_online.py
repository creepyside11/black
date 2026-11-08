import time
from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

chat_lines = []

@emitter.on("onShowChat")
def on_chat(bot_obj, msg):
    chat_lines.append(msg)
    print(f"[CHAT] {msg}")

b.start()
print("[*] Ждем коннекта и авторизации...")
for _ in range(15):
    time.sleep(1)
    if "заспавнен" in b.status or "авторизован" in b.status or (b.session and b.session.connected):
        break

time.sleep(2)

# Проверяем, какой ID выдал сервер боту
my_id = None
if b.session and hasattr(b.session, "players") and hasattr(b.session.players, "bot"):
    my_id = getattr(b.session.players.bot, "id", None)

print(f"[+] НАШ НИК: {b.nickname}")
print(f"[+] НАШ ID НА СЕРВЕРЕ: {my_id}")

# Отправляем /id Hesinko
print("[*] Отправляем команду /id Hesinko...")
b.send_chat("/id Hesinko")
time.sleep(2)

# Отправляем /id Sha_Pismenskij (себя)
print("[*] Отправляем команду /id Sha_Pismenskij...")
b.send_chat("/id Sha_Pismenskij")
time.sleep(2)

print("\n--- Ответы сервера ---")
for l in chat_lines:
    if any(k in l.lower() for k in ["hesinko", "sha_pismenskij", "совпаден", "найден", "игрок", "id:"]):
        print("  ->", l)

b.stop()
