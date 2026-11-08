import time
import sys
import os
import threading
import logging
from bot import BotManager

# Выбираем 4 бота на 4 разных серверах: TVER (#1), VORONEZH (#12), VOLOGDA (#36), PENZA (#41)
SELECTED_IDS = [1, 12, 36, 41]

def main():
    print(f"[*] Инициализация BotManager для запуска {len(SELECTED_IDS)} ботов: {SELECTED_IDS}")
    manager = BotManager()
    
    print("[*] Запуск ботов с интервалом между стартами...")
    manager.start_selected(SELECTED_IDS)
    
    print(f"[+] Все {len(SELECTED_IDS)} ботов запущены в фоновом режиме. Начинаем мониторинг...")
    start_time = time.time()
    
    try:
        while True:
            time.sleep(20)
            elapsed_min = int((time.time() - start_time) / 60)
            online = 0
            waiting = 0
            connecting = 0
            other = 0
            
            statuses = []
            for bid in SELECTED_IDS:
                b = manager.bots.get(bid)
                if not b:
                    continue
                st = b.status
                statuses.append(f"#{bid}({b.server_name}): {st} [{b.level_str}]")
                if "В игре" in st:
                    online += 1
                elif "Ожидание" in st:
                    waiting += 1
                elif "Подключение" in st:
                    connecting += 1
                else:
                    other += 1
                    
            print(f"\n[+{elapsed_min}m STATUS] Онлайн: {online}/{len(SELECTED_IDS)} | Ожидание: {waiting} | Подключение: {connecting} | Прочее: {other}")
            for st in statuses:
                print(f"  -> {st}")

            # Если есть команда в cmd.txt, выполняем её
            if os.path.exists("cmd.txt"):
                try:
                    with open("cmd.txt", "r") as cf:
                        cmd = cf.read().strip()
                    if cmd:
                        print(f"[CMD] Получена команда: {cmd}")
                        os.remove("cmd.txt")
                        parts = cmd.split(maxsplit=1)
                        action = parts[0]
                        arg = parts[1] if len(parts) > 1 else ""
                        if action == "drive_bank":
                            target_bid = int(arg) if arg.isdigit() else 12
                            bot = manager.bots.get(target_bid)
                            if bot and bot.api:
                                def _run_drive(b):
                                    pos = b.api.getbotposition()
                                    b.add_log(f"[*] Начало поездки в Банк Арзамаса. Текущая позиция: {pos}")
                                    stream = b.session.stream
                                    pickups = getattr(stream, "pickups", {})
                                    b.add_log(f"[*] Вокруг обнаружено пикапов: {len(pickups)}, авто: {len(getattr(stream, 'vehicles', {}))}")
                                    # Аренда скутера
                                    from logic.bot_tasks import MinerTask
                                    dummy_miner = MinerTask(b)
                                    b.add_log("[*] Поиск пикапа аренды скутера...")
                                    rented = dummy_miner._find_and_rent_scooter()
                                    time.sleep(2.0)
                                    # Едем в Банк г. Арзамас (координаты Центра/Банка: X=2280.0, Y=-1450.0, Z=25.0)
                                    bank_x, bank_y, bank_z = 2280.0, -1450.0, 25.0
                                    mode = "vehicle" if b.session.players.bot.is_in_vehicle() else "walk"
                                    b.add_log(f"[*] Направление в Банк Арзамаса ({bank_x}, {bank_y}, {bank_z}) в режиме {mode}...")
                                    try:
                                        b.api.MoveToCoord(bank_x, bank_y, bank_z, mode=mode, coord_delay=0.03)
                                        b.add_log("[+] Успешно прибыли к Банку города Арзамас!")
                                    except Exception as ex:
                                        b.add_log(f"[-] Ошибка пути в банк: {ex}")
                                threading.Thread(target=_run_drive, args=(bot,), daemon=True).start()

                        elif action == "create_account":
                            target_bid = int(arg) if arg.isdigit() else 12
                            bot = manager.bots.get(target_bid)
                            if bot and bot.api:
                                def _run_bank_acc(b):
                                    b.add_log("[БАНК] Начинаем процедуру входа и создания счета...")
                                    # 1. Если сидим на скутере - спешиваемся
                                    if b.session.players.bot.is_in_vehicle():
                                        b.add_log("[БАНК] Спешиваемся со скутера...")
                                        try:
                                            b.api.exit_vehicle()
                                        except Exception:
                                            pass
                                        time.sleep(1.5)

                                    # 2. Ищем пикап входа или пикап банка рядом
                                    stream = b.session.stream
                                    pickups = getattr(stream, "pickups", {})
                                    b.add_log(f"[БАНК] Сканирование пикапов рядом (всего {len(pickups)})...")
                                    cur_pos = b.api.getbotposition() or (0, 0, 0)
                                    bx, by, bz = cur_pos[0], cur_pos[1], cur_pos[2]
                                    
                                    nearest_p = None
                                    min_d = 999.0
                                    for pid, p in list(pickups.items()):
                                        px, py, pz = float(getattr(p, 'x', 0)), float(getattr(p, 'y', 0)), float(getattr(p, 'z', 0))
                                        d = ((px - bx)**2 + (py - by)**2 + (pz - bz)**2)**0.5
                                        if d < min_d:
                                            min_d = d
                                            nearest_p = (pid, px, py, pz)
                                    
                                    if nearest_p and min_d < 30.0:
                                        pid, px, py, pz = nearest_p
                                        b.add_log(f"[БАНК] Идем на ближайший пикап #{pid} ({min_d:.1f}м)...")
                                        b.api.MoveToCoord(px, py, pz, mode="walk", completion_distance=0.8)
                                        time.sleep(0.5)
                                        b.api.SendPickup(pid)
                                        b.api.SetKey(keys=1)
                                        time.sleep(0.2)
                                        b.api.SetKey(keys=0)
                                        time.sleep(2.0)

                                    # 3. Отправляем /bank
                                    b.add_log("[БАНК] Отправляем команду /bank...")
                                    b.send_chat("/bank")
                                    time.sleep(1.5)

                                    # Нажимаем 'Создать счет' (обычно кнопка/пункт 0 или диалог)
                                    # Пин-код 0000
                                    from samp.packets import PacketBuilder
                                    # Отвечаем в появившийся диалог создания счета
                                    time.sleep(1.0)
                                    b.add_log("[БАНК] Вводим пин-код 0000 в диалог создания...")
                                    try:
                                        # Отправляем подтверждение и пин
                                        b.session._sender.send(
                                            PacketBuilder.rpc_build("dialog_response", 0, 1, 0, "0000"),
                                            rpc_name="dialog_response"
                                        )
                                        b.session._sender.send(
                                            PacketBuilder.rpc_build("dialog_response", 1, 1, 0, "0000"),
                                            rpc_name="dialog_response"
                                        )
                                    except Exception as e:
                                        b.add_log(f"[БАНК] Ошибка диалога: {e}")
                                threading.Thread(target=_run_bank_acc, args=(bot,), daemon=True).start()
                except Exception as e:
                    print(f"[!] Ошибка исполнения команды: {e}")

            # Печатаем свежие логи для активных ботов
            for bid in SELECTED_IDS:
                b = manager.bots.get(bid)
                if not b:
                    continue
                if not hasattr(b, "_last_printed_log_len"):
                    b._last_printed_log_len = 0
                current_logs = list(b.logs)
                if len(current_logs) > b._last_printed_log_len:
                    new_entries = current_logs[b._last_printed_log_len:]
                    b._last_printed_log_len = len(current_logs)
                    print(f"--- [LOGS #{bid} {b.nickname} @ {b.server_name}] ---")
                    for entry in new_entries:
                        print(f"    {entry}")
            sys.stdout.flush()
            
    except KeyboardInterrupt:
        print("[*] Остановка фермы...")
        manager.stop_all()

if __name__ == "__main__":
    main()
