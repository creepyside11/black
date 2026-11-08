import time
import math
import os
import sys
from bot import BotManager
from samp.interfaces import InterfaceID
from samp.packets import PacketBuilder

m = BotManager()

SPAWNS = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813)
}

def identify_spawn(pos):
    if not pos or len(pos) < 3 or (pos[0] == 0.0 and pos[1] == 0.0) or pos[2] < -5.0:
        return None
    for name, s_pos in SPAWNS.items():
        dist = math.hypot(pos[0] - s_pos[0], pos[1] - s_pos[1])
        if dist < 85.0:
            return name, dist
    return f"Карта ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})", 999.0

# Боты на VORONEZH
bot_candidates = [12, 13, 15, 16]

iteration = 1
while True:
    print(f"\n=======================================================")
    print(f"🔄 ИТЕРАЦИЯ #{iteration}: Пробуем ботов на спавн Нижегородск 2")
    print(f"=======================================================")
    
    for bid in bot_candidates:
        b = m.bots.get(bid)
        if not b or b.is_banned:
            continue
            
        print(f"\n[*] Пробуем бота #{bid} [{b.nickname}]...")
        b.start()
        
        # Ждем логина
        for _ in range(12):
            time.sleep(1)
            if "заспавнен" in b.status or "авторизован" in b.status:
                break
                
        # Шлем выбор спавна и spawn
        time.sleep(1)
        if b.session and b.session.connected:
            try:
                b.session.send_json(InterfaceID.SPAWN_SELECT, {"t": 1, "s": 0})
                b.session.send_json(InterfaceID.SPAWN_SELECT, {"s": 0})
                b.session._sender.send(PacketBuilder.rpc_build("spawn"), rpc_name="spawn")
                time.sleep(0.3)
                if b.api:
                    b.api.SetKey(keys=32)
                    time.sleep(0.1)
                    b.api.SetKey(keys=0)
            except Exception:
                pass
                
        time.sleep(3)
        pos = b.api.getbotposition() if b.api else None
        res = identify_spawn(pos)
        
        if res:
            name, dist = res
            streamed = b.session.players.get_streamed_players() if (b.session and hasattr(b.session, 'players')) else {}
            real_players = [p for p in streamed.values() if not getattr(p, 'is_npc', False)]
            
            print(f"[+] Бот #{bid} [{b.nickname}] заспавнен на: {name} (расстояние {dist:.1f}м, игроков рядом: {len(real_players)})")
            
            if "Нижегородск 2" in name:
                print("\n" + "🎯"*25)
                print(f"НАЙДЕН НИЖЕГОРОДСК 2! Бот #{bid} [{b.nickname}] на вокзале!")
                print(f"Координаты: {pos}")
                print(f"Живых игроков в стриме: {len(real_players)}")
                print("НЕ ВЫХОДИМ! Ждем подтверждения владельца...")
                print("🎯"*25 + "\n")
                
                # Сохраняем маркер
                with open("nizhe2_success.txt", "w") as f:
                    f.write(f"{bid}|{b.nickname}|{pos[0]}|{pos[1]}|{pos[2]}")
                    
                # Держим бота онлайн на сервере
                while True:
                    time.sleep(1)
            else:
                print(f"[-] Спавн: {name}. Нужен Нижегородск 2. Выходим...")
                b.stop()
                print("[*] Ждем 20 секунд перед следующим аккаунтом...")
                time.sleep(20.0)
        else:
            print(f"[-] Бот #{bid} не появился на открытом спавне (pos={pos}). Выходим...")
            b.stop()
            print("[*] Ждем 20 секунд перед следующим аккаунтом...")
            time.sleep(20.0)
            
    iteration += 1
