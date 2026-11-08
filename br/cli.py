import os
import sys
import time
import math
import re
import threading
from br.storage import AccountStorage
from br.client import InteractiveBot
import br_servers

class ConsoleApp:
    def __init__(self):
        self.storage = AccountStorage()
        self.active_bot = None
        self.running = True

    def run(self):
        self.print_banner()
        while self.running:
            if not self.active_bot:
                self.main_menu()
            else:
                self.bot_session_menu()

    def print_banner(self):
        print("=" * 60)
        print("   BLACK RUSSIA INTERACTIVE CLIENT (BR)")
        print("=" * 60)

    def main_menu(self):
        print("\n--- СПИСОК АККАУНТОВ ---")
        accs = self.storage.list_accounts()
        if not accs:
            print("  (Аккаунтов нет. Добавьте новый через меню)")
        else:
            for idx, acc in enumerate(accs):
                srv = acc.get("server", "?")
                nick = acc.get("nickname", "?")
                lvl = acc.get("lvl", "-")
                money = acc.get("money", 0)
                px = f" [Прокси: {acc['proxy']}]" if acc.get("proxy") else ""
                print(f" [{idx + 1}] {nick} @ {srv} | LVL: {lvl} | Баланс: {money:,} руб.{px}")

        print("\nКоманды:")
        print("  <номер>       - Выбрать аккаунт для входа в игру")
        print("  add           - Добавить новый аккаунт (ник, сервер, пароль)")
        print("  pass <номер>  - Сменить сохраненный пароль аккаунта")
        print("  del <номер>   - Удалить аккаунт из списка")
        print("  exit          - Выход")

        try:
            choice = input("\n[BR] > ").strip()
        except (EOFError, KeyboardInterrupt):
            self.running = False
            return

        if not choice:
            return

        parts = choice.split()
        cmd = parts[0].lower()

        if cmd in ("exit", "quit", "q"):
            self.running = False
            return

        elif cmd == "add":
            self.action_add_account()

        elif cmd == "pass":
            if len(parts) >= 2 and parts[1].isdigit():
                self.action_change_password(int(parts[1]) - 1)
            else:
                print("Использование: pass <номер аккаунта>")

        elif cmd == "del":
            if len(parts) >= 2 and parts[1].isdigit():
                self.action_delete_account(int(parts[1]) - 1)
            else:
                print("Использование: del <номер аккаунта>")

        elif choice.isdigit():
            idx = int(choice) - 1
            acc = self.storage.get_account(idx)
            if acc:
                self.start_bot_session(acc)
            else:
                print("[-] Неверный номер аккаунта!")

        else:
            print("[-] Неизвестная команда.")

    def action_add_account(self):
        print("\n--- Добавление аккаунта ---")
        try:
            nick = input("Введите никнейм (например, Ivan_Ivanov): ").strip()
            if not nick:
                print("[-] Отменено.")
                return

            srv = input("Введите сервер (номер или название, например 1 или RED): ").strip()
            if not srv:
                print("[-] Отменено.")
                return

            # Проверяем сервер
            s_info = br_servers.find_server(srv)
            if not s_info:
                print(f"[-] Сервер '{srv}' не найден!")
                return
            srv_canonical = s_info.get("name", srv).upper()

            pwd = input("Введите пароль: ").strip()
            if not pwd:
                print("[-] Пароль обязателен!")
                return

            self.storage.add_account(srv_canonical, nick, pwd)
            print(f"[+] Аккаунт {nick} на сервере {srv_canonical} успешно сохранен!")

        except (EOFError, KeyboardInterrupt):
            print("\n[-] Отменено.")

    def action_change_password(self, index: int):
        acc = self.storage.get_account(index)
        if not acc:
            print("[-] Аккаунт не найден!")
            return
        print(f"\nСмена пароля для {acc['nickname']} @ {acc['server']}:")
        try:
            new_pwd = input("Введите новый пароль: ").strip()
            if new_pwd:
                self.storage.update_password(index, new_pwd)
                print("[+] Пароль успешно обновлен!")
            else:
                print("[-] Пароль не может быть пустым.")
        except (EOFError, KeyboardInterrupt):
            print("\n[-] Отменено.")

    def action_delete_account(self, index: int):
        acc = self.storage.get_account(index)
        if not acc:
            print("[-] Аккаунт не найден!")
            return
        confirm = input(f"Удалить аккаунт {acc['nickname']} @ {acc['server']}? (y/N): ").strip().lower()
        if confirm == "y":
            self.storage.delete_account(index)
            print("[+] Аккаунт удален.")

    def start_bot_session(self, acc: dict):
        srv = acc.get("server")
        nick = acc.get("nickname")
        pwd = acc.get("password")
        px = acc.get("proxy", "")

        def _on_update(event_type, data):
            if event_type == "stats" and self.active_bot:
                self.storage.update_stats(nick, srv, self.active_bot.level_str, self.active_bot.money)

        try:
            self.active_bot = InteractiveBot(srv, nick, pwd, proxy=px, on_update_cb=_on_update)
            self.active_bot.start()
        except Exception as e:
            print(f"[-] Ошибка запуска: {e}")
            self.active_bot = None

    def bot_session_menu(self):
        b = self.active_bot
        if not b:
            return

        print("\n" + "=" * 60)
        cur_pos = b.get_position()
        veh_str = " (В ТРАНСПОРТЕ)" if b.is_in_vehicle() else " (ПЕШКОМ)"
        print(f"🤖 АКТИВНЫЙ БОТ: {b.nickname} @ {b.server_name} [{b.status}]")
        print(f"💰 БАЛАНС: {b.money:,} руб. | 🎖 LVL: {b.level_str}{veh_str}")
        print(f"📍 Позиция: X={cur_pos[0]:.1f}, Y={cur_pos[1]:.1f}, Z={cur_pos[2]:.1f}")

        if b.active_checkpoint:
            cx, cy, cz, cr = b.active_checkpoint
            dist = math.sqrt((cur_pos[0]-cx)**2 + (cur_pos[1]-cy)**2 + (cur_pos[2]-cz)**2)
            print(f"🎯 МЕТКА / GPS: X={cx:.1f}, Y={cy:.1f}, Z={cz:.1f} (дистанция {dist:.1f}м)")
        print("=" * 60)

        # Если есть активный диалог на экране бота - показываем его и интерактивные кнопки!
        if b.active_dialog:
            self.display_active_dialog(b.active_dialog)

        print("\nКоманды управления:")
        print("  /chat <текст>   - Отправить сообщение или команду (например, /chat /mm или /chat /gps)")
        print("  chat            - Открыть режим живого чата")
        print("  1               - ИДТИ/ЕХАТЬ к активной метке GPS (обход препятствий)")
        print("  stop            - Остановить движение бота")
        print("  pickups         - Показать ближайшие пикапы (входы, скутеры, маркеры)")
        print("  goto <id>       - Подойти к пикапу и активировать его (войти / взять скутер)")
        print("  stats           - Обновить баланс и уровень (/mm)")
        print("  back            - Отключиться от сервера и вернуться в список аккаунтов")

        try:
            cmd_line = input(f"\n[{b.nickname}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            cmd_line = "back"

        if not cmd_line:
            return

        if cmd_line.lower() in ("back", "exit", "disconnect"):
            print("[*] Отключение бота...")
            b.stop()
            self.active_bot = None
            return

        if cmd_line.lower() == "chat":
            self.live_chat_mode(b)
            return

        if cmd_line.startswith("/chat "):
            sub_cmd = cmd_line[6:].strip()
            b.send_command(sub_cmd)
            time.sleep(1.0)
            return

        # Если пользователь просто ввел команду с чертой, например /mm или /gps
        if cmd_line.startswith("/"):
            b.send_command(cmd_line)
            time.sleep(1.0)
            return

        # Если на экране диалог, и пользователь ввел цифру или ответ
        if b.active_dialog:
            handled = self.handle_dialog_input(b, cmd_line)
            if handled:
                return

        # Навигация к метке GPS по нажатию '1'
        if cmd_line == "1":
            if b.active_checkpoint:
                cx, cy, cz, cr = b.active_checkpoint
                print(f"[*] Запуск следования к метке ({cx:.1f}, {cy:.1f}, {cz:.1f})...")
                b.navigate_to(cx, cy, cz, radius=cr)
            else:
                print("[-] Сейчас нет активной метки GPS! Откройте /chat /gps и выберите пункт.")
            return

        if cmd_line.lower() == "stop":
            b.stop_movement()
            print("[*] Движение остановлено.")
            return

        if cmd_line.lower() == "pickups":
            self.display_nearby_pickups(b)
            return

        if cmd_line.lower().startswith("goto "):
            parts = cmd_line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                pid = int(parts[1])
                b.interact_with_pickup(pid)
            else:
                print("Использование: goto <ID пикапа>")
            return

        if cmd_line.lower() == "stats":
            b.send_command("/mm")
            print("[*] Запрос /mm отправлен. Ожидаем ответ сервера...")
            time.sleep(1.5)
            return

        print(f"[-] Неизвестная команда '{cmd_line}'. Для отправки в чат игры используйте: /chat {cmd_line}")

    def display_active_dialog(self, d: dict):
        print(f"\n┌── [ДИАЛОГ НА ЭКРАНЕ БОТА: {d['title']}] ───────────────")
        items = d.get("items", [])
        if items:
            print("│ Выберите пункт (введите номер):")
            for idx, item_text in items:
                print(f"│  [{idx}] {item_text}")
        else:
            # Текстовый диалог или поле ввода
            for line in d.get("info", "").split("\n"):
                print(f"│  {line}")

        b1 = d.get("b1") or "OK"
        b2 = d.get("b2") or ""
        btn_str = f"Кнопки: [enter / ok: {b1}]"
        if b2:
            btn_str += f" | [esc / cancel: {b2}]"
        print(f"└── {btn_str} ─────────────────────────────")

    def handle_dialog_input(self, b: InteractiveBot, inp: str) -> bool:
        d = b.active_dialog
        if not d:
            return False

        # Если это выбор пункта списка
        if inp.isdigit():
            val = int(inp)
            items = d.get("items", [])
            # Если такой номер пункта есть в диалоге
            if any(it[0] == val for it in items):
                print(f"[*] Выбран пункт [{val}] в диалоге...")
                b.send_dialog_response(response=1, list_item=val, input_text="")
                time.sleep(0.8)
                return True

        if inp.lower() in ("ok", "yes", "да", "1") and d.get("style") not in (2, 4, 5):
            b.send_dialog_response(response=1, list_item=0, input_text=inp if inp not in ("ok", "1") else "")
            time.sleep(0.8)
            return True

        if inp.lower() in ("cancel", "close", "esc", "нет", "0"):
            print("[*] Закрытие диалога...")
            b.send_dialog_response(response=0, list_item=0, input_text="")
            time.sleep(0.8)
            return True

        # Если стиль 1 или 3 (ввод текста)
        if d.get("style") in (1, 3):
            b.send_dialog_response(response=1, list_item=0, input_text=inp)
            time.sleep(0.8)
            return True

        return False

    def display_nearby_pickups(self, b: InteractiveBot):
        pickups = b.get_nearby_pickups(radius=80.0)
        print("\n--- БЛИЖАЙШИЕ ПИКАПЫ / ОБЪЕКТЫ ---")
        if not pickups:
            print("  (В радиусе 80м пикапов не найдено)")
            return

        for p in pickups[:15]:
            pid = p["id"]
            model = p["model"]
            dist = p["dist"]
            desc = "Маркер/Пикап"
            # Известные модели пикапов в SAMP
            if model in (1239, 1272, 1273, 19134):
                desc = "Вход в здание / дверь"
            elif model in (1274, 1212):
                desc = "Деньги / зарплата / банкомат"
            elif model in (1318, 1277):
                desc = "Чекпоинт / маркер интерьера"
            elif model in (1275, 1240, 1242):
                desc = "Одежда / броня / здоровье"
            print(f"  [ID {pid:4d}] {desc:25s} | Дистанция: {dist:4.1f}м (Модель {model})")
        print("\nЧтобы бот подошел к пикапу и взял его, введите: goto <ID>")

    def live_chat_mode(self, b: InteractiveBot):
        print("\n--- РЕЖИМ ЖИВОГО ЧАТА (для выхода отправьте /back или /exit) ---")
        with b._lock:
            for line in list(b.chat_history)[-15:]:
                print(line)

        while True:
            try:
                line = input(f"[ЧАТ @ {b.nickname}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not line:
                continue

            if line.lower() in ("/back", "/exit", "back", "exit"):
                break

            b.send_command(line)
            time.sleep(0.5)

def main():
    app = ConsoleApp()
    app.run()

if __name__ == "__main__":
    main()
