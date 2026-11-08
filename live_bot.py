import time
import sys
from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

print("[*] Запускаем Sha_Pismenskij...")
b.start()

# Ждем подключения и спавна
for i in range(20):
    time.sleep(1)
    if b.session and getattr(b.session.players.bot, "is_spawned", False):
        break

time.sleep(2)
bot_p = b.session.players.bot
print(f"[+] Спавн успешен! Позиция: X={bot_p.x:.2f}, Y={bot_p.y:.2f}, Z={bot_p.z:.2f}")

# Отправляем сообщение в чат
time.sleep(1)
b.send_chat("Я ТУТ! Помашите рукой!")
print("[+] Отправлено в чат: 'Я ТУТ! Помашите рукой!'")

# Проверим, кого бот видит рядом
streamed = b.session.players.get_streamed_players()
print(f"[+] Игроков в радиусе: {len(streamed)}")
for pid, p in list(streamed.items())[:15]:
    pname = getattr(p, "name", f"ID_{pid}")
    dist = ((p.x - bot_p.x)**2 + (p.y - bot_p.y)**2 + (p.z - bot_p.z)**2)**0.5
    print(f"    -> {pname} [ID {pid}] dist: {dist:.1f}м ({p.x:.1f}, {p.y:.1f}, {p.z:.1f})")

print("\n[*] Запускаем активную анимацию, прыжки и приседания на 90 секунд...")
# Цикл активных действий, чтобы бот точно не пропадал из поля зрения
start = time.time()
while time.time() - start < 90:
    # Прыжок
    b.api.SetKey(keys=32)
    time.sleep(0.3)
    b.api.SetKey(keys=0)
    time.sleep(1.0)
    
    # Приседание
    b.api.SetKey(keys=2)
    time.sleep(0.5)
    b.api.SetKey(keys=0)
    time.sleep(1.0)
    
    # Шаг вперед-назад
    cur = b.api.getbotposition()
    if cur:
        b.api.MoveToCoord(cur[0] + 0.8, cur[1] + 0.8, cur[2], mode="walk", completion_distance=0.2)
        time.sleep(1.0)
        b.api.MoveToCoord(cur[0] - 0.8, cur[1] - 0.8, cur[2], mode="walk", completion_distance=0.2)
        time.sleep(1.0)

print("[*] Время истекло, завершаем.")
b.stop()
