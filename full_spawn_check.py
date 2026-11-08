import time
import re
from bot import BotManager
from logic.events import emitter
from samp.packets import PacketBuilder

m = BotManager()
b = m.bots.get(12) # Sha_Pismenskij @ VORONEZH

dialogs_seen = []

@emitter.on("onShowDialog")
def dialog_logger(bot_obj, dialog_id, style, title, b1, b2, text):
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', text)
    dialogs_seen.append((dialog_id, style, title, b1, b2, clean))
    print(f"\n[ВСПЛЫЛ ДИАЛОГ #{dialog_id}] Title='{title}' | Buttons: '{b1}'/'{b2}'\nText: {clean[:200]}")
    # Скипаем любое всплывающее окно (кнопка 1 или 0)
    time.sleep(0.3)
    try:
        b.session._sender.send(
            PacketBuilder.rpc_build("dialog_response", int(dialog_id), 1, 0, ""),
            rpc_name="dialog_response",
        )
        print(f"[+] Скипнут диалог #{dialog_id}")
    except Exception as e:
        print(f"[-] Ошибка скипа диалога: {e}")

@emitter.on("onShowChat")
def chat_logger(bot_obj, msg):
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', msg).strip()
    if any(k in clean.lower() for k in ["спавн", "выбор", "заспавн", "войти", "пароль", "кик", "бан", "сервер"]):
        print(f"[ЧАТ] {clean}")

print("[*] Запускаем бота и подключаемся...")
b.start()

# Ждем 12 секунд для полной инициализации
for i in range(12):
    time.sleep(1)
    if b.session and getattr(b.session.players.bot, "is_spawned", False):
        print(f"[*] Флаг спавна установлен на {i} сек.")

time.sleep(2)

# Проверяем реальное состояние персонажа
bot_p = getattr(b.session.players, "bot", None)
if bot_p:
    print(f"\n[ИНФО О ПЕРСОНАЖЕ]")
    print(f"  - is_spawned: {getattr(bot_p, 'is_spawned', False)}")
    print(f"  - skin: {getattr(bot_p, 'skin', None)}")
    print(f"  - health: {getattr(bot_p, 'health', None)}")
    print(f"  - interior: {getattr(bot_p, 'interior', None)}")
    print(f"  - virtual_world: {getattr(bot_p, 'virtual_world', None)}")
    print(f"  - position: ({bot_p.x:.2f}, {bot_p.y:.2f}, {bot_p.z:.2f})")

# Шлем RPC_SPAWN и RequestClass на всякий случай, если он в Spectate/Class Selection
print("\n[*] Отправляем RequestSpawn и Spawn...")
try:
    from samp.packets import PacketBuilder
    b.session._sender.send(PacketBuilder.rpc_build("request_spawn"), rpc_name="request_spawn")
    b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
except Exception as e:
    print(f"[-] Ошибка отправки spawn: {e}")

time.sleep(2)

# Отправляем OnFoot синхронизацию прямо в координаты вокзала Лыткарино (чтобы другие игроки гарантированно увидели скин)
print("[*] Начинаем слать OnFoot Sync и прыгаем...")
b.api.SetKey(keys=32)
time.sleep(0.3)
b.api.SetKey(keys=0)
time.sleep(0.5)

# Проверяем игроков рядом (кто видит нас и кого видим мы)
streamed = b.session.players.get_streamed_players()
print(f"\n[+] Игроков в зоне стрима вокруг бота: {len(streamed)}")
for pid, p in list(streamed.items())[:10]:
    pname = getattr(p, "name", f"ID_{pid}")
    px, py, pz = getattr(p, "x", 0), getattr(p, "y", 0), getattr(p, "z", 0)
    dist = ((px - bot_p.x)**2 + (py - bot_p.y)**2 + (pz - bot_p.z)**2)**0.5
    print(f"    - Игрок {pname} [ID {pid}] на расст. {dist:.1f}м ({px:.1f}, {py:.1f}, {pz:.1f})")

# Напишем в локальный чат сообщение
b.send_chat("я тут на спавне")
print("[+] Отправлено в чат: 'я тут на спавне'")

time.sleep(10)
b.stop()
