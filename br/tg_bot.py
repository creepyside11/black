import os
import sys
import time
import math
import re
import threading
import telebot
from telebot import types

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from br.storage import AccountStorage
from br.client import InteractiveBot
import br_servers

class TelegramInteractiveController:
    def __init__(self, token: str, admin_id: int | None = None):
        self.token = token.strip()
        self.admin_id = admin_id
        self.bot = telebot.TeleBot(self.token, parse_mode="HTML")
        self.storage = AccountStorage()
        self.active_bot: InteractiveBot | None = None
        self.user_states = {}
        self.chat_subscribers = set()
        if self.admin_id:
            self.chat_subscribers.add(self.admin_id)

        self._setup_handlers()

    def _is_authorized(self, user_id: int) -> bool:
        if self.admin_id is None:
            return True
        return user_id == self.admin_id

    def run(self):
        print(f"[*] Telegram бот запущен (@{(self.bot.get_me()).username})...")
        self.bot.infinity_polling(skip_pending=True)

    def _notify(self, chat_id: int, text: str, markup=None):
        try:
            self.bot.send_message(chat_id, text, reply_markup=markup)
        except Exception:
            pass

    def _on_bot_event(self, event_type: str, data):
        if not self.active_bot:
            return

        if event_type == "dialog":
            # Пришел новый диалог на экран бота -> отправляем клавиатуру с кнопками диалога в Telegram!
            d = self.active_bot.active_dialog
            if not d:
                return

            text = f"📋 <b>Диалог на экране:</b> <code>{d['title']}</code>\n\n"
            items = d.get("items", [])
            markup = types.InlineKeyboardMarkup(row_width=1)

            if items:
                for idx, text_item in items[:25]:  # до 25 кнопок
                    btn_title = f"{idx}. {text_item[:35]}"
                    markup.add(types.InlineKeyboardButton(btn_title, callback_data=f"br_dselect:{idx}"))
            else:
                text += f"<i>{d.get('info', '')[:300]}</i>\n\n"

            # Основные кнопки подтверждения/отмены
            b1 = d.get("b1") or "OK"
            b2 = d.get("b2") or "Отмена"
            row_btns = [types.InlineKeyboardButton(f"✅ {b1}", callback_data="br_dok")]
            if b2:
                row_btns.append(types.InlineKeyboardButton(f"❌ {b2}", callback_data="br_dcancel"))
            markup.row(*row_btns)

            for cid in list(self.chat_subscribers):
                self._notify(cid, text, markup=markup)

        elif event_type == "checkpoint":
            if data:
                cx, cy, cz, cr = data
                msg = (
                    f"🎯 <b>Сервер установил метку GPS!</b>\n"
                    f"Координаты: <code>{cx:.1f}, {cy:.1f}, {cz:.1f}</code> (Радиус: {cr})\n"
                    f"Нажмите кнопку ниже или отправьте <code>1</code>, чтобы бот пошел/поехал туда!"
                )
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🚀 1 — Идти/Ехать к метке", callback_data="br_goto_gps"))
                for cid in list(self.chat_subscribers):
                    self._notify(cid, msg, markup=markup)

        elif event_type == "stats":
            self.storage.update_stats(
                self.active_bot.nickname,
                self.active_bot.server_name,
                self.active_bot.level_str,
                self.active_bot.money
            )

        elif event_type == "chat":
            pass

    def _setup_handlers(self):
        @self.bot.message_handler(commands=["start", "menu"])
        def cmd_start(message):
            if not self._is_authorized(message.from_user.id):
                self.bot.reply_to(message, "⛔️ Доступ запрещен.")
                return

            self.chat_subscribers.add(message.chat.id)
            self.send_main_menu(message.chat.id)

        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_callbacks(call):
            if not self._is_authorized(call.from_user.id):
                self.bot.answer_callback_query(call.id, "⛔️ Нет доступа", show_alert=True)
                return

            data = call.data
            chat_id = call.message.chat.id

            if data == "menu_main":
                self.send_main_menu(chat_id, message_id=call.message.message_id)
                self.bot.answer_callback_query(call.id)

            elif data == "menu_add":
                self.user_states[chat_id] = {"action": "add_step_nick"}
                self.bot.send_message(chat_id, "Введите никнейм персонажа (например: <code>Ivan_Ivanov</code>):")
                self.bot.answer_callback_query(call.id)

            elif data.startswith("acc_view:"):
                idx = int(data.split(":")[1])
                self.send_account_view(chat_id, idx, message_id=call.message.message_id)
                self.bot.answer_callback_query(call.id)

            elif data.startswith("acc_connect:"):
                idx = int(data.split(":")[1])
                acc = self.storage.get_account(idx)
                if not acc:
                    self.bot.answer_callback_query(call.id, "Аккаунт не найден!")
                    return

                if self.active_bot:
                    self.active_bot.stop()
                    self.active_bot = None

                self.bot.answer_callback_query(call.id, f"Подключение к {acc['nickname']}...")
                self.active_bot = InteractiveBot(
                    acc["server"],
                    acc["nickname"],
                    acc["password"],
                    proxy=acc.get("proxy", ""),
                    on_update_cb=self._on_bot_event
                )
                self.active_bot.start()
                self.send_active_session_view(chat_id)

            elif data.startswith("acc_change_pass:"):
                idx = int(data.split(":")[1])
                self.user_states[chat_id] = {"action": "change_pass", "idx": idx}
                self.bot.send_message(chat_id, "Введите новый пароль для этого аккаунта:")
                self.bot.answer_callback_query(call.id)

            elif data.startswith("acc_delete:"):
                idx = int(data.split(":")[1])
                self.storage.delete_account(idx)
                self.bot.answer_callback_query(call.id, "Аккаунт удален!")
                self.send_main_menu(chat_id, message_id=call.message.message_id)

            elif data == "br_active_view":
                self.send_active_session_view(chat_id, message_id=call.message.message_id)
                self.bot.answer_callback_query(call.id)

            elif data == "br_stop_bot":
                if self.active_bot:
                    self.active_bot.stop()
                    self.active_bot = None
                self.bot.answer_callback_query(call.id, "Бот отключен!")
                self.send_main_menu(chat_id, message_id=call.message.message_id)

            elif data == "br_chat_log":
                if not self.active_bot:
                    self.bot.answer_callback_query(call.id, "Бот не активен!")
                    return
                with self.active_bot._lock:
                    lines = list(self.active_bot.chat_history)[-20:]
                log_text = "<b>💬 Чат игры (последние строки):</b>\n\n" + "\n".join(lines if lines else ["(Чат пуст)"])
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="br_chat_log"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="br_active_view"))
                self.bot.send_message(chat_id, log_text, reply_markup=markup)
                self.bot.answer_callback_query(call.id)

            elif data == "br_goto_gps":
                if not self.active_bot or not self.active_bot.active_checkpoint:
                    self.bot.answer_callback_query(call.id, "Метка GPS не выставлена!")
                    return
                cx, cy, cz, cr = self.active_bot.active_checkpoint
                self.active_bot.navigate_to(cx, cy, cz, radius=cr)
                self.bot.answer_callback_query(call.id, "Движение к метке GPS начато!")

            elif data == "br_stop_move":
                if self.active_bot:
                    self.active_bot.stop_movement()
                self.bot.answer_callback_query(call.id, "Движение остановлено!")

            elif data == "br_pickups":
                if not self.active_bot:
                    self.bot.answer_callback_query(call.id, "Бот не активен!")
                    return
                pickups = self.active_bot.get_nearby_pickups(radius=80.0)
                if not pickups:
                    self.bot.send_message(chat_id, "📍 В радиусе 80м пикапов/маркеров не найдено.")
                    self.bot.answer_callback_query(call.id)
                    return

                text = "<b>📍 Ближайшие пикапы:</b>\n\n"
                markup = types.InlineKeyboardMarkup(row_width=1)
                for p in pickups[:10]:
                    pid = p["id"]
                    dist = p["dist"]
                    model = p["model"]
                    desc = "Маркер"
                    if model in (1239, 1272, 1273, 19134):
                        desc = "🚪 Вход в здание"
                    elif model in (1274, 1212):
                        desc = "💵 Банкомат / Деньги"
                    elif model in (1318, 1277):
                        desc = "🎯 Чекпоинт"
                    text += f"• ID {pid}: {desc} ({dist}м)\n"
                    markup.add(types.InlineKeyboardButton(f"🚶 Подойти к #{pid} ({desc})", callback_data=f"br_pickup_go:{pid}"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="br_active_view"))
                self.bot.send_message(chat_id, text, reply_markup=markup)
                self.bot.answer_callback_query(call.id)

            elif data.startswith("br_pickup_go:"):
                pid = int(data.split(":")[1])
                if self.active_bot:
                    self.active_bot.interact_with_pickup(pid)
                    self.bot.answer_callback_query(call.id, f"Идем к пикапу #{pid}...")
                else:
                    self.bot.answer_callback_query(call.id, "Бот не активен!")

            elif data.startswith("br_dselect:"):
                item_idx = int(data.split(":")[1])
                if self.active_bot and self.active_bot.active_dialog:
                    self.active_bot.send_dialog_response(response=1, list_item=item_idx, input_text="")
                    self.bot.answer_callback_query(call.id, f"Выбран пункт {item_idx}")
                else:
                    self.bot.answer_callback_query(call.id, "Диалог уже закрыт")

            elif data == "br_dok":
                if self.active_bot and self.active_bot.active_dialog:
                    self.active_bot.send_dialog_response(response=1, list_item=0, input_text="")
                    self.bot.answer_callback_query(call.id, "Нажато OK")
                else:
                    self.bot.answer_callback_query(call.id, "Диалог закрыт")

            elif data == "br_dcancel":
                if self.active_bot and self.active_bot.active_dialog:
                    self.active_bot.send_dialog_response(response=0, list_item=0, input_text="")
                    self.bot.answer_callback_query(call.id, "Нажато Отмена")
                else:
                    self.bot.answer_callback_query(call.id, "Диалог закрыт")

        @self.bot.message_handler(func=lambda msg: True)
        def handle_text(message):
            if not self._is_authorized(message.from_user.id):
                return

            chat_id = message.chat.id
            text = message.text.strip()
            state = self.user_states.get(chat_id)

            if state:
                action = state.get("action")
                if action == "add_step_nick":
                    state["nick"] = text
                    state["action"] = "add_step_server"
                    self.bot.send_message(chat_id, "Введите номер или название сервера (например, <code>1</code> или <code>RED</code>):")
                    return

                elif action == "add_step_server":
                    s_info = br_servers.find_server(text)
                    if not s_info:
                        self.bot.send_message(chat_id, f"❌ Сервер '{text}' не найден! Попробуйте снова:")
                        return
                    state["server"] = s_info.get("name", text).upper()
                    state["action"] = "add_step_pass"
                    self.bot.send_message(chat_id, "Введите пароль от аккаунта:")
                    return

                elif action == "add_step_pass":
                    nick = state["nick"]
                    srv = state["server"]
                    pwd = text
                    self.storage.add_account(srv, nick, pwd)
                    del self.user_states[chat_id]
                    self.bot.send_message(chat_id, f"✅ Аккаунт <b>{nick}</b> @ <b>{srv}</b> успешно добавлен в список!")
                    self.send_main_menu(chat_id)
                    return

                elif action == "change_pass":
                    idx = state["idx"]
                    del self.user_states[chat_id]
                    self.storage.update_password(idx, text)
                    self.bot.send_message(chat_id, "✅ Пароль успешно обновлен!")
                    self.send_account_view(chat_id, idx)
                    return

            # Если пользователь отправил '1' — идет к метке GPS
            if text == "1":
                if self.active_bot and self.active_bot.active_checkpoint:
                    cx, cy, cz, cr = self.active_bot.active_checkpoint
                    self.active_bot.navigate_to(cx, cy, cz, radius=cr)
                    self.bot.reply_to(message, "🚀 Запущено движение к метке GPS (цифра 1)!")
                else:
                    self.bot.reply_to(message, "Метка GPS не выставлена! Сначала выберите пункт в /chat /gps")
                return

            # Если пользователь написал команду: /chat /mm или просто /mm или /gps
            if text.startswith("/chat ") or text.startswith("/"):
                cmd_to_send = text[6:].strip() if text.startswith("/chat ") else text
                if self.active_bot:
                    self.active_bot.send_command(cmd_to_send)
                    self.bot.reply_to(message, f"📤 Отправлено боту: <code>{cmd_to_send}</code>")
                else:
                    self.bot.reply_to(message, "❌ Бот не активен! Выберите аккаунт и подключитесь.")
                return

            # Если у бота открыт диалог ввода (style 1 или 3), передаем ввод в диалог
            if self.active_bot and self.active_bot.active_dialog:
                d = self.active_bot.active_dialog
                if d.get("style") in (1, 3):
                    self.active_bot.send_dialog_response(response=1, list_item=0, input_text=text)
                    self.bot.reply_to(message, f"📥 Введено в диалог: <code>{text}</code>")
                    return

            # Обычный текст -> отправка в игровой чат
            if self.active_bot:
                self.active_bot.send_command(text)
                self.bot.reply_to(message, f"💬 Отправлено в чат: {text}")

    def send_main_menu(self, chat_id: int, message_id: int | None = None):
        accs = self.storage.list_accounts()
        text = "<b>🎮 Black Russia Interactive Bot</b>\n\n<b>Список ваших аккаунтов:</b>\n"
        markup = types.InlineKeyboardMarkup(row_width=1)

        if not accs:
            text += "<i>(Аккаунтов нет. Добавьте первый аккаунт!)</i>\n"
        else:
            for idx, a in enumerate(accs):
                lvl = a.get("lvl", "-")
                money = a.get("money", 0)
                btn_title = f"{idx+1}. {a['nickname']} @ {a['server']} [{lvl} | {money:,}₽]"
                markup.add(types.InlineKeyboardButton(btn_title, callback_data=f"acc_view:{idx}"))

        markup.add(types.InlineKeyboardButton("➕ Добавить аккаунт", callback_data="menu_add"))
        if self.active_bot:
            markup.add(types.InlineKeyboardButton("🟢 Открыть активного бота", callback_data="br_active_view"))

        if message_id:
            try:
                self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                return
            except Exception:
                pass
        self.bot.send_message(chat_id, text, reply_markup=markup)

    def send_account_view(self, chat_id: int, idx: int, message_id: int | None = None):
        acc = self.storage.get_account(idx)
        if not acc:
            return
        text = (
            f"👤 <b>Аккаунт: {acc['nickname']}</b>\n"
            f"Сервер: <b>{acc['server']}</b>\n"
            f"Уровень: <b>{acc.get('lvl', '-')}</b>\n"
            f"Баланс: <b>{acc.get('money', 0):,} руб.</b>\n"
            f"Пароль: <code>{acc.get('password')}</code>\n"
            f"Прокси: <code>{acc.get('proxy') or 'Свой IP'}</code>"
        )
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.row(
            types.InlineKeyboardButton("▶️ Зайти на аккаунт", callback_data=f"acc_connect:{idx}"),
            types.InlineKeyboardButton("🔑 Сменить пароль", callback_data=f"acc_change_pass:{idx}")
        )
        markup.row(
            types.InlineKeyboardButton("❌ Удалить", callback_data=f"acc_delete:{idx}"),
            types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu_main")
        )
        if message_id:
            try:
                self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                return
            except Exception:
                pass
        self.bot.send_message(chat_id, text, reply_markup=markup)

    def send_active_session_view(self, chat_id: int, message_id: int | None = None):
        b = self.active_bot
        if not b:
            self.send_main_menu(chat_id, message_id=message_id)
            return

        cur_pos = b.get_position()
        veh_str = " (В машине/скутере)" if b.is_in_vehicle() else " (Пешком)"
        gps_str = ""
        if b.active_checkpoint:
            cx, cy, cz, cr = b.active_checkpoint
            dist = math.sqrt((cur_pos[0]-cx)**2 + (cur_pos[1]-cy)**2 + (cur_pos[2]-cz)**2)
            gps_str = f"\n🎯 <b>GPS метка:</b> {cx:.1f}, {cy:.1f}, {cz:.1f} ({dist:.1f}м)"

        text = (
            f"🤖 <b>Активный персонаж: {b.nickname}</b>\n"
            f"Сервер: <b>{b.server_name}</b>\n"
            f"Статус: <b>{b.status}</b>{veh_str}\n"
            f"Баланс: <b>{b.money:,} руб.</b> | Уровень: <b>{b.level_str}</b>\n"
            f"Позиция: <code>X={cur_pos[0]:.1f}, Y={cur_pos[1]:.1f}, Z={cur_pos[2]:.1f}</code>{gps_str}\n\n"
            f"<i>Подсказка: напишите /chat /mm или /chat /gps для открытия меню. Нажмите '1' для следования к метке GPS.</i>"
        )

        markup = types.InlineKeyboardMarkup(row_width=2)
        if b.active_checkpoint:
            markup.row(types.InlineKeyboardButton("🚀 1 — Идти/Ехать к метке", callback_data="br_goto_gps"))

        markup.row(
            types.InlineKeyboardButton("💬 Смотреть чат", callback_data="br_chat_log"),
            types.InlineKeyboardButton("📍 Ближайшие пикапы", callback_data="br_pickups")
        )
        markup.row(
            types.InlineKeyboardButton("🛑 Стоп движение", callback_data="br_stop_move"),
            types.InlineKeyboardButton("⏹ Выйти из игры", callback_data="br_stop_bot")
        )
        markup.row(types.InlineKeyboardButton("🔄 Обновить экран", callback_data="br_active_view"))

        if message_id:
            try:
                self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                return
            except Exception:
                pass
        self.bot.send_message(chat_id, text, reply_markup=markup)
