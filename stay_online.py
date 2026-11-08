import time
import sys
import re
from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

print(f"[*] Запуск бота #{b.bot_id} [{b.nickname}] на {b.server_name}...")

@emitter.on("onShowChat")
def on_chat(bot_obj, msg):
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', msg).strip()
    print(f"[ЧАТ СЕРВЕРА] {clean}")
    sys.stdout.flush()

b.start()

# Ждем авторизации
for i in range(25):
    time.sleep(1)
    if "В игре" in b.status:
        break

print(f"[+] Статус бота: {b.status}")
sys.stdout.flush()

# Основной бесконечный цикл удержания онлайна
# Бот НЕ выходит, постоянно обновляет стрим-зону, делает микро-движения
while True:
    try:
        time.sleep(10)
        bot_p = getattr(b.session.players, "bot", None) if b.session else None
        pos = b.api.getbotposition() if b.api else None
        streamed = b.session.players.get_streamed_players() if b.session else {}
        
        print("\n" + "="*50)
        print(f"[ОНЛАЙН СТАТУС] {time.strftime('%H:%M:%S')} | Статус: {b.status}")
        if pos:
            print(f"  -> Координаты: X={pos[0]:.2f}, Y={pos[1]:.2f}, Z={pos[2]:.2f}")
        print(f"  -> Игроков в зоне стрима вокруг: {len(streamed)}")
        
        for pid, p in list(streamed.items())[:10]:
            pname = getattr(p, "name", f"ID_{pid}")
            px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
            if pos:
                d = ((px - pos[0])**2 + (py - pos[1])**2 + (pz - pos[2])**2)**0.5
                print(f"     * {pname} [ID {pid}] | Дистанция: {d:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")
            else:
                print(f"     * {pname} [ID {pid}] ({px:.1f}, {py:.1f}, {pz:.1f})")
        print("="*50)
        sys.stdout.flush()
        
        # Микро-действие для сброса АФК
        if b.api:
            b.api.SetKey(keys=2)
            time.sleep(0.3)
            b.api.SetKey(keys=0)
            
    except Exception as e:
        print(f"[-] Ошибка цикла: {e}")
        time.sleep(5)
