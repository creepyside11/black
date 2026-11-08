import time
import sys
import os
import re
from bot import BotManager
from logic.events import emitter

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

print(f"[*] Старт контроллера для бота #{b.bot_id} [{b.nickname}] на {b.server_name}...")

@emitter.on("onShowChat")
def on_chat(bot_obj, msg):
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', msg).strip()
    print(f"[ЧАТ] {clean}")
    sys.stdout.flush()

b.start()

# Ждем авторизации и спавна
for i in range(25):
    time.sleep(1)
    if "В игре" in b.status:
        break

time.sleep(2)
pos = b.api.getbotposition() if b.api else None
print(f"[ГОТОВ К КОМАНДАМ] Бот в игре! Позиция: {pos}")
sys.stdout.flush()

# Цикл ожидания команд из файла manual_cmd.txt
while True:
    try:
        if os.path.exists("manual_cmd.txt"):
            with open("manual_cmd.txt", "r", encoding="utf-8") as f:
                cmd = f.read().strip()
            if cmd:
                os.remove("manual_cmd.txt")
                print(f"[ВЫПОЛНЕНИЕ КОМАНДЫ]: {cmd}")
                parts = cmd.split(maxsplit=1)
                c = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if c == "chat":
                    b.send_chat(arg)
                    print(f"[+] Сказали в чат: {arg}")
                elif c == "jump":
                    b.api.SetKey(keys=32)
                    time.sleep(0.4)
                    b.api.SetKey(keys=0)
                    print("[+] Прыгнули")
                elif c == "crouch":
                    b.api.SetKey(keys=2)
                    time.sleep(0.5)
                    b.api.SetKey(keys=0)
                    print("[+] Присели")
                elif c == "setpos":
                    coords = [float(x) for x in arg.split()]
                    print(f"[*] Мгновенно перемещаемся на {coords}...")
                    b.session.players.bot.x = coords[0]
                    b.session.players.bot.y = coords[1]
                    b.session.players.bot.z = coords[2]
                    b.api.SetKey(keys=32)
                    time.sleep(0.3)
                    b.api.SetKey(keys=0)
                elif c == "goto":
                    coords = [float(x) for x in arg.split()]
                    print(f"[*] Идем на {coords}...")
                    b.api.MoveToCoord(coords[0], coords[1], coords[2], mode="walk")
                elif c == "pos":
                    cur = b.api.getbotposition()
                    print(f"[+] Текущие координаты: {cur}")
                elif c == "players":
                    streamed = b.session.players.get_streamed_players() if b.session else {}
                    print(f"\n[+] Игроков в зоне стрима: {len(streamed)}")
                    cur = b.api.getbotposition() or (0, 0, 0)
                    for pid, p in list(streamed.items())[:15]:
                        pname = getattr(p, "name", f"ID_{pid}")
                        px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
                        d = ((px - cur[0])**2 + (py - cur[1])**2 + (pz - cur[2])**2)**0.5
                        print(f"    * {pname} [ID {pid}] | Дистанция: {d:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")
                elif c == "drive_lytkarino_bank":
                    print("[*] Начинаем процедуру аренды скутера в Лыткарино...")
                    from logic.bot_tasks import MinerTask
                    mt = MinerTask(b)
                    rented = mt._find_and_rent_scooter()
                    time.sleep(2.5)
                    # Координаты отделения Банка г. Лыткарино (центральный проспект):
                    bank_x, bank_y, bank_z = 2415.0, -2460.0, 13.6
                    mode = "vehicle" if b.session.players.bot.is_in_vehicle() else "walk"
                    print(f"[*] Едем в Банк Лыткарино ({bank_x}, {bank_y}, {bank_z}) в режиме {mode}...")
                    b.api.MoveToCoord(bank_x, bank_y, bank_z, mode=mode, coord_delay=0.03)
                    time.sleep(1.0)
                    print(f"[+] Прибыли к Банку Лыткарино! Текущая позиция: {b.api.getbotposition()}")
                sys.stdout.flush()

        time.sleep(1)
        # Легкий анти-афк тик раз в 20 сек
        if int(time.time()) % 20 == 0:
            if b.api:
                b.api.SetKey(keys=2)
                time.sleep(0.2)
                b.api.SetKey(keys=0)

    except Exception as e:
        print(f"[-] Ошибка цикла: {e}")
        time.sleep(2)
