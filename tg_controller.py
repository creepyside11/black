import threading
import time
import math
import os
import telebot
from telebot import types

BOTS_PER_PAGE = 8
PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")

def load_proxy_list():
    proxies = []
    if os.path.exists(PROXIES_FILE):
        try:
            with open(PROXIES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "|" in line:
                        name, px = line.split("|", 1)
                        proxies.append((name.strip(), px.strip()))
                    else:
                        proxies.append((line, line))
        except Exception:
            pass
    return proxies

def save_proxy_to_file(name: str, proxy_str: str):
    try:
        with open(PROXIES_FILE, "a", encoding="utf-8") as f:
            f.write(f"{name}|{proxy_str}\n")
    except Exception:
        pass

class TelegramBotController:
    def __init__(self, token: str, manager, admin_id: int | None = None):
        self.token = token.strip()
        self.manager = manager
        self.admin_id = admin_id  # Если указан, команды принимает только админ
        self.bot = telebot.TeleBot(self.token, parse_mode="HTML")
        self.user_states = {}  # {chat_id: {"action": "...", "data": {...}}}
        self.active_chats = set()
        if self.admin_id:
            self.active_chats.add(self.admin_id)
        self.thread = None
        self.running = False
        self._setup_handlers()

    def broadcast(self, text: str):
        for cid in list(self.active_chats):
            try:
                self.bot.send_message(cid, text)
            except Exception:
                pass

    def _is_authorized(self, user_id: int) -> bool:
        if self.admin_id is None:
            return True
        return user_id == self.admin_id

    def _format_status_badge(self, status: str) -> str:
        s = status.lower()
        if "забанен" in s:
            return "🔴 БАН"
        elif "в игре" in s or "заспавнен" in s or "авторизован" in s:
            return "🟢 Онлайн"
        elif "подключение" in s or "реконнект" in s:
            return "🟡 Коннект..."
        elif "остановлен" in s or "отключен" in s:
            return "⚪️ Остановлен"
        return f"⚪️ {status}"

    def _build_pagination_keyboard(self, page: int = 1):
        with self.manager._lock:
            bot_items = sorted(self.manager.bots.items(), key=lambda x: x[0])
        total_bots = len(bot_items)
        total_pages = max(1, math.ceil(total_bots / BOTS_PER_PAGE))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * BOTS_PER_PAGE
        end_idx = start_idx + BOTS_PER_PAGE
        page_bots = bot_items[start_idx:end_idx]

        markup = types.InlineKeyboardMarkup(row_width=2)

        # Кнопки со списком ботов на странице
        bot_buttons = []
        for bid, b in page_bots:
            status_ico = self._format_status_badge(b.status)
            btn_text = f"#{bid} {b.nickname} [{status_ico}]"
            bot_buttons.append(types.InlineKeyboardButton(btn_text, callback_data=f"bot_view:{bid}:{page}"))
        
        # Размещаем ботов парами или по одному
        for i in range(0, len(bot_buttons), 2):
            if i + 1 < len(bot_buttons):
                markup.row(bot_buttons[i], bot_buttons[i+1])
            else:
                markup.row(bot_buttons[i])

        # Кнопки пагинации
        nav_buttons = []
        if page > 1:
            if page > 3:
                nav_buttons.append(types.InlineKeyboardButton("⏪ -3", callback_data=f"page:{max(1, page-3)}"))
            nav_buttons.append(types.InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}"))

        nav_buttons.append(types.InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data=f"refresh:{page}"))

        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("➡️", callback_data=f"page:{page+1}"))
            if page + 3 <= total_pages:
                nav_buttons.append(types.InlineKeyboardButton("+3 ⏩", callback_data=f"page:{min(total_pages, page+3)}"))

        markup.row(*nav_buttons)

        # Быстрые глобальные действия
        markup.row(
            types.InlineKeyboardButton("▶️ Запустить выбранных", callback_data=f"act_start_some:{page}"),
            types.InlineKeyboardButton("▶️ Всех", callback_data="act_startall"),
            types.InlineKeyboardButton("⏹ Всех", callback_data="act_stopall")
        )
        markup.row(
            types.InlineKeyboardButton("➕ Массовое создание", callback_data="act_batch"),
            types.InlineKeyboardButton("🌐 Список Прокси", callback_data="act_proxies_list")
        )
        markup.row(
            types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"page:{page}")
        )
        return markup, total_bots, total_pages

    def _setup_handlers(self):
        @self.bot.message_handler(commands=["start", "menu", "help"])
        def cmd_start(message):
            if not self._is_authorized(message.from_user.id):
                self.bot.reply_to(message, "⛔️ Доступ запрещен. Укажите ваш Telegram ID администратора.")
                return

            self.active_chats.add(message.chat.id)
            markup, total_bots, total_pages = self._build_pagination_keyboard(1)
            
            # Считаем количество ботов по статусам
            online = 0
            banned = 0
            with self.manager._lock:
                for b in self.manager.bots.values():
                    if "в игре" in b.status.lower() or "заспавнен" in b.status.lower():
                        online += 1
                    elif b.is_banned:
                        banned += 1

            text = (
                f"<b>🤖 Black Russia Bot Manager</b>\n"
                f"Всего ботов: <b>{total_bots}</b> | Онлайн: <b>{online}</b> | Забанено: <b>{banned}</b>\n\n"
                f"Выберите бота для детальной инфы, логов и управления:"
            )
            self.bot.send_message(message.chat.id, text, reply_markup=markup)

        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_callbacks(call):
            if not self._is_authorized(call.from_user.id):
                self.bot.answer_callback_query(call.id, "⛔️ Нет доступа", show_alert=True)
                return

            data = call.data
            chat_id = call.message.chat.id
            message_id = call.message.message_id

            if data.startswith("page:"):
                page = int(data.split(":")[1])
                markup, total_bots, total_pages = self._build_pagination_keyboard(page)
                text = (
                    f"<b>🤖 Список ботов (Страница {page}/{total_pages})</b>\n"
                    f"Всего аккаунтов в системе: <b>{total_bots}</b>\n\n"
                    f"Нажмите на нужного бота для логов и управления:"
                )
                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data.startswith("refresh:"):
                page = int(data.split(":")[1])
                markup, total_bots, total_pages = self._build_pagination_keyboard(page)
                try:
                    self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id, "Обновлено!")

            elif data.startswith("act_start_some:"):
                page = int(data.split(":")[1])
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "start_selected_bots", "page": page}
                help_msg = (
                    "<b>▶️ Запуск нескольких существующих ботов</b>\n\n"
                    "Введите ID ботов через пробел или запятую, либо диапазон:\n"
                    "Примеры:\n"
                    "• <code>1 2 5 8</code>\n"
                    "• <code>1-10</code> (запустит ботов с ID от 1 до 10)\n"
                    "• <code>1-5, 8, 12-15</code>\n\n"
                    "Для отмены отправьте /cancel"
                )
                self.bot.send_message(chat_id, help_msg)

            elif data == "act_startall":
                self.bot.answer_callback_query(call.id, "Запуск всех ботов...")
                threading.Thread(target=self.manager.start_all, daemon=True).start()
                self.bot.send_message(chat_id, "🚀 Подан сигнал на запуск всех доступных ботов!")

            elif data == "act_stopall":
                self.bot.answer_callback_query(call.id, "Остановка ботов...")
                self.manager.stop_all()
                self.bot.send_message(chat_id, "⏹ Все боты остановлены.")

            elif data == "act_batch":
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "batch_create"}
                help_msg = (
                    "<b>➕ Массовое создание ботов</b>\n\n"
                    "Отправьте параметры одной строкой через пробел:\n"
                    "<code>[сервер] [кол-во] [пароль] [задача]</code>\n\n"
                    "Пример:\n"
                    "<code>TVER 5 mypass123 lvl</code>\n"
                    "Задачи на выбор: <code>lvl</code>, <code>quest</code>, <code>mine</code>, <code>factory</code>, <code>bus</code>\n\n"
                    "Для отмены отправьте /cancel"
                )
                self.bot.send_message(chat_id, help_msg)

            elif data.startswith("bot_view:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.answer_callback_query(call.id, "Бот не найден!", show_alert=True)
                    return

                loc = self.manager._get_bot_location_name(b)
                loc_str = f"\n📍 Локация: <code>{loc}</code>" if loc else ""
                proxy_str = f"\n🌐 Прокси: <code>{b.proxy}</code>" if b.proxy else ""
                task_str = b.active_task.name if b.active_task else (b.default_task_name or "нет")

                goal_str = f"\n🎯 Цель: <b>{b.target_lvl} lvl</b>" if b.target_lvl else "\n🎯 Цель: <i>не задана</i>"

                text = (
                    f"<b>👤 Информация о боте #{b.bot_id}</b>\n"
                    f"Никнейм: <b>{b.nickname}</b>\n"
                    f"Сервер: <b>{b.server_name}</b> ({b.host}:{b.port})\n"
                    f"Статус: <b>{self._format_status_badge(b.status)}</b>\n"
                    f"Уровень: <b>{b.get_level()}</b>\n"
                    f"Баланс: <b>{b.get_balance_str()}</b>\n"
                    f"Задача: <code>{task_str}</code>{goal_str}{loc_str}{proxy_str}\n"
                    f"Пароль: <code>{b.password}</code>"
                )

                markup = types.InlineKeyboardMarkup(row_width=2)
                # Кнопки действий
                btn_start_stop = types.InlineKeyboardButton("▶️ Запустить", callback_data=f"bot_start:{bid}:{page}")
                if "в игре" in b.status.lower() or "подключение" in b.status.lower():
                    btn_start_stop = types.InlineKeyboardButton("⏹ Остановить", callback_data=f"bot_stop:{bid}:{page}")

                markup.row(btn_start_stop, types.InlineKeyboardButton("📜 Логи", callback_data=f"bot_logs:{bid}:{page}"))
                markup.row(
                    types.InlineKeyboardButton("🔄 Обновить статы (/mm)", callback_data=f"bot_check_stats:{bid}:{page}"),
                    types.InlineKeyboardButton("🎯 Задать задачу", callback_data=f"bot_tasks:{bid}:{page}")
                )
                markup.row(
                    types.InlineKeyboardButton("🏆 Задать цель (LVL)", callback_data=f"bot_set_goal:{bid}:{page}"),
                    types.InlineKeyboardButton("💬 Чат / Команда", callback_data=f"bot_chat:{bid}:{page}")
                )
                markup.row(
                    types.InlineKeyboardButton("🌐 Прокси", callback_data=f"bot_proxy_menu:{bid}:{page}"),
                    types.InlineKeyboardButton("❌ Удалить бота", callback_data=f"bot_del:{bid}:{page}")
                )

                if b.is_banned:
                    markup.row(types.InlineKeyboardButton("🔓 Снять бан", callback_data=f"bot_unban:{bid}:{page}"))

                markup.row(types.InlineKeyboardButton("⬅️ Назад к списку", callback_data=f"page:{page}"))

                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data.startswith("bot_start:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.manager.start_bot(bid)
                self.bot.answer_callback_query(call.id, f"Бот #{bid} запускается...")
                # Обновляем окно бота
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_stop:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.manager.stop_bot(bid)
                self.bot.answer_callback_query(call.id, f"Бот #{bid} остановлен.")
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_unban:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.manager.unban_bot(bid)
                self.bot.answer_callback_query(call.id, f"Бот #{bid} разбанен!")
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_check_stats:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)
                if b and "в игре" in b.status.lower():
                    b.stats_pending = True
                    b.send_chat("/mm")
                    if b.api and b.session and b.session.connected:
                        try:
                            b.api.sendscoresandpings()
                        except Exception:
                            pass
                    self.bot.answer_callback_query(call.id, "Запрос /mm отправлен серверу! Обновляю...", show_alert=False)
                    time.sleep(1.0)
                else:
                    self.bot.answer_callback_query(call.id, "Бот не в игре!", show_alert=True)
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_set_goal:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.answer_callback_query(call.id, "Бот не найден!")
                    return

                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "set_bot_goal", "bid": bid, "page": page}
                cur_goal = f"{b.target_lvl} lvl" if b.target_lvl else "не задана"
                msg = (
                    f"🎯 <b>Настройка цели для бота #{bid} [{b.nickname}]</b>\n\n"
                    f"Текущая цель: <b>{cur_goal}</b>\n\n"
                    f"Отправьте желаемый целевой уровень (число, например: <code>4</code>).\n"
                    f"Когда бот достигнет этого уровня, в Telegram придет уведомление, а бот остановится.\n\n"
                    f"Чтобы сбросить цель, отправьте <code>0</code> или <code>нет</code>.\n"
                    f"Для отмены: /cancel"
                )
                self.bot.send_message(chat_id, msg)

            elif data.startswith("bot_del:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.manager.delete_bot(bid)
                self.bot.answer_callback_query(call.id, f"Бот #{bid} удален.")
                call.data = f"page:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_logs:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.answer_callback_query(call.id, "Бот не найден!")
                    return

                logs_list = list(b.logs)
                if not logs_list:
                    log_text = "<i>(Логов пока нет)</i>"
                else:
                    # Берем последние 15 записей логов
                    tail = logs_list[-15:]
                    # Чистим теги HTML
                    cleaned = [l.replace("<", "&lt;").replace(">", "&gt;") for l in tail]
                    log_text = "\n".join(cleaned)

                text = f"<b>📜 Последние логи #{b.bot_id} [{b.nickname}]:</b>\n\n<code>{log_text}</code>"
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.row(
                    types.InlineKeyboardButton("🔄 Обновить логи", callback_data=f"bot_logs:{bid}:{page}"),
                    types.InlineKeyboardButton("⬅️ Назад к боту", callback_data=f"bot_view:{bid}:{page}")
                )
                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data.startswith("bot_tasks:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.row(
                    types.InlineKeyboardButton("Качать LVL (АФК)", callback_data=f"set_task:{bid}:{page}:lvl"),
                    types.InlineKeyboardButton("Квесты", callback_data=f"set_task:{bid}:{page}:quest")
                )
                markup.row(
                    types.InlineKeyboardButton("Шахта (Miner)", callback_data=f"set_task:{bid}:{page}:mine"),
                    types.InlineKeyboardButton("Завод (Factory)", callback_data=f"set_task:{bid}:{page}:factory")
                )
                markup.row(
                    types.InlineKeyboardButton("Автобусник", callback_data=f"set_task:{bid}:{page}:bus"),
                    types.InlineKeyboardButton("⏹ Снять задачу", callback_data=f"set_task:{bid}:{page}:stop")
                )
                markup.row(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"bot_view:{bid}:{page}"))
                self.bot.edit_message_text(f"Выберите задачу для бота #{bid}:", chat_id=chat_id, message_id=message_id, reply_markup=markup)
                self.bot.answer_callback_query(call.id)

            elif data.startswith("set_task:"):
                _, bid_s, page_s, t_name = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)
                if b:
                    res = b.set_task(t_name)
                    self.bot.answer_callback_query(call.id, f"Задача: {res}", show_alert=True)
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_chat:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.user_states[chat_id] = {"action": "send_chat", "bid": bid, "page": page}
                self.bot.send_message(chat_id, f"Введите текст сообщения или команду для отправки ботом #{bid}:\n(Например: <code>/mm</code> или <code>Привет всем</code>)\nДля отмены: /cancel")
                self.bot.answer_callback_query(call.id)

            elif data.startswith("bot_proxy_menu:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.answer_callback_query(call.id, "Бот не найден!")
                    return

                cur_px = b.proxy if b.proxy else "Не установлен (Свой IP)"
                text = (
                    f"<b>🌐 Настройка прокси для бота #{b.bot_id} [{b.nickname}]</b>\n\n"
                    f"Текущий прокси: <code>{cur_px}</code>\n\n"
                    f"Выберите сохраненный прокси из списка или введите новый вручную:"
                )

                markup = types.InlineKeyboardMarkup(row_width=1)
                saved_proxies = load_proxy_list()
                for name, px_val in saved_proxies[:6]:
                    markup.add(types.InlineKeyboardButton(f"⚡️ {name}", callback_data=f"apply_px:{bid}:{page}:{name}"))

                markup.add(types.InlineKeyboardButton("✏️ Ввести прокси вручную", callback_data=f"bot_proxy_custom:{bid}:{page}"))
                markup.add(types.InlineKeyboardButton("🚫 Сбросить прокси (Свой IP)", callback_data=f"bot_proxy_reset:{bid}:{page}"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад к боту", callback_data=f"bot_view:{bid}:{page}"))

                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data.startswith("apply_px:"):
                _, bid_s, page_s, px_name = data.split(":", 3)
                bid = int(bid_s)
                page = int(page_s)
                saved_proxies = dict(load_proxy_list())
                target_px = saved_proxies.get(px_name, "")
                if target_px:
                    self.manager.set_bot_proxy(bid, target_px)
                    self.bot.answer_callback_query(call.id, f"Прокси '{px_name}' применен!", show_alert=True)
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_proxy_reset:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.manager.set_bot_proxy(bid, "none")
                self.bot.answer_callback_query(call.id, "Прокси сброшен!", show_alert=True)
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("bot_proxy_custom:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                self.user_states[chat_id] = {"action": "set_custom_proxy", "bid": bid, "page": page}
                msg = (
                    "<b>🌐 Ввод прокси</b>\n\n"
                    "Отправьте прокси в формате:\n"
                    "<code>ip:port:user:pass</code>\n"
                    "или\n"
                    "<code>socks5://user:pass@ip:port</code>\n\n"
                    "Также можно задать имя и сохранить в список:\n"
                    "<code>Название|ip:port:user:pass</code>\n\n"
                    "Для отмены отправьте /cancel"
                )
                self.bot.send_message(chat_id, msg)
                self.bot.answer_callback_query(call.id)

            elif data == "act_proxies_list":
                saved_proxies = load_proxy_list()
                text = "<b>🌐 Список сохраненных прокси:</b>\n\n"
                if not saved_proxies:
                    text += "<i>(Список пока пуст)</i>\nДобавить можно кнопкой ниже."
                else:
                    for name, px_val in saved_proxies:
                        masked = px_val.split("@")[-1] if "@" in px_val else px_val.split(":")[0] + ":***"
                        text += f"• <b>{name}</b>: <code>{masked}</code>\n"

                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(types.InlineKeyboardButton("➕ Добавить прокси в список", callback_data="act_add_saved_proxy"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="page:1"))

                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data == "act_add_saved_proxy":
                self.user_states[chat_id] = {"action": "add_global_proxy"}
                msg = (
                    "<b>➕ Добавление прокси в список</b>\n\n"
                    "Отправьте имя и прокси через разделитель <code>|</code>:\n"
                    "<code>МойПрокси1|ip:port:login:password</code>\n\n"
                    "Для отмены отправьте /cancel"
                )
                self.bot.send_message(chat_id, msg)
                self.bot.answer_callback_query(call.id)

        @self.bot.message_handler(commands=["cancel"])
        def cmd_cancel(message):
            if message.chat.id in self.user_states:
                del self.user_states[message.chat.id]
            self.bot.reply_to(message, "Действие отменено.")

        @self.bot.message_handler(func=lambda msg: True)
        def handle_user_text(message):
            if not self._is_authorized(message.from_user.id):
                return

            state = self.user_states.get(message.chat.id)
            if not state:
                return

            action = state.get("action")
            if action == "batch_create":
                del self.user_states[message.chat.id]
                parts = message.text.strip().split()
                if len(parts) < 3:
                    self.bot.reply_to(message, "❌ Неверный формат! Нужно: <code>СЕРВЕР КОЛИЧЕСТВО ПАРОЛЬ [ЗАДАЧА]</code>")
                    return

                srv = parts[0].upper()
                if not parts[1].isdigit():
                    self.bot.reply_to(message, "❌ Количество должно быть числом!")
                    return

                count = int(parts[1])
                pwd = parts[2]
                task = parts[3].lower() if len(parts) >= 4 else "lvl"

                if count > 50:
                    self.bot.reply_to(message, "⚠️ Максимальное количество для одной партии - 50 ботов.")
                    return

                status_msg = self.bot.send_message(message.chat.id, f"⏳ Запущен процесс генерации и подключения {count} ботов на сервер {srv}...")

                def _batch_worker():
                    created = self.manager.add_bots_batch(srv, count, password=pwd, default_task=task)
                    self.bot.send_message(
                        message.chat.id,
                        f"✅ Успешно создано {len(created)} ботов на сервере <b>{srv}</b> с задачей <code>{task}</code>!\nОткройте /start для просмотра списка."
                    )

                threading.Thread(target=_batch_worker, daemon=True).start()

            elif action == "start_selected_bots":
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                raw_input = message.text.strip()
                ids_to_start = set()

                tokens = re.split(r'[\s,]+', raw_input)
                for t in tokens:
                    if not t:
                        continue
                    if "-" in t:
                        parts = t.split("-", 1)
                        if parts[0].isdigit() and parts[1].isdigit():
                            r_start, r_end = int(parts[0]), int(parts[1])
                            if r_start > r_end:
                                r_start, r_end = r_end, r_start
                            for x in range(r_start, r_end + 1):
                                ids_to_start.add(x)
                    elif t.isdigit():
                        ids_to_start.add(int(t))

                if not ids_to_start:
                    self.bot.reply_to(message, "❌ Не распознано ни одного ID бота. Пример: <code>1-5, 8, 10</code>")
                    return

                selected_list = sorted(list(ids_to_start))
                started = self.manager.start_selected(selected_list)
                if started:
                    names_str = ", ".join([f"#{b.bot_id} [{b.nickname}]" for b in started[:10]])
                    if len(started) > 10:
                        names_str += f" и еще {len(started) - 10}..."
                    self.bot.reply_to(
                        message,
                        f"🚀 Запущены выбранные боты ({len(started)} шт.):\n{names_str}\n\nПроверить список: /start"
                    )
                else:
                    self.bot.reply_to(message, "⚠️ Выбранные боты не найдены или уже запущены/забанены.")

            elif action == "set_bot_goal":
                bid = state.get("bid")
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                raw_val = message.text.strip().lower()

                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.reply_to(message, f"❌ Бот #{bid} не найден!")
                    return

                if raw_val in ("0", "нет", "отмена", "сброс", "none", "-"):
                    b.target_lvl = None
                    b.target_achieved = False
                    self.manager.save_accounts()
                    self.bot.reply_to(message, f"🎯 Цель для бота #{bid} [{b.nickname}] сброшена.")
                else:
                    m = re.search(r'\d+', raw_val)
                    if m:
                        target = int(m.group(0))
                        b.target_lvl = target
                        b.target_achieved = False
                        self.manager.save_accounts()
                        self.bot.reply_to(
                            message,
                            f"✅ Для бота #{bid} [{b.nickname}] установлена цель: <b>{target} lvl</b>!\n"
                            f"При достижении уровня вам придет уведомление, а бот остановится."
                        )
                        b.check_target_level_achieved()
                    else:
                        self.bot.reply_to(message, "❌ Не удалось распознать уровень. Укажите число (например, 4).")

            elif action == "send_chat":
                bid = state.get("bid")
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                text_to_send = message.text.strip()

                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b or "в игре" not in b.status.lower():
                    self.bot.reply_to(message, f"❌ Бот #{bid} не находится в игре!")
                    return

                b.send_chat(text_to_send)
                self.bot.reply_to(message, f"✅ Отправлено от имени бота #{bid} [{b.nickname}]:\n<code>{text_to_send}</code>")

            elif action == "set_custom_proxy":
                bid = state.get("bid")
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                raw_val = message.text.strip()

                # Если указали Имя|ip:port...
                if "|" in raw_val:
                    p_name, p_proxy = raw_val.split("|", 1)
                    p_name = p_name.strip()
                    p_proxy = p_proxy.strip()
                    save_proxy_to_file(p_name, p_proxy)
                    apply_val = p_proxy
                    self.bot.reply_to(message, f"💾 Прокси сохранен в список как <b>{p_name}</b>!")
                else:
                    apply_val = raw_val

                self.manager.set_bot_proxy(bid, apply_val)
                self.bot.send_message(
                    message.chat.id,
                    f"✅ Прокси <code>{apply_val}</code> успешно назначен боту #{bid}!\nТеперь можно запустить его через /start."
                )

            elif action == "add_global_proxy":
                del self.user_states[message.chat.id]
                raw_val = message.text.strip()
                if "|" not in raw_val:
                    self.bot.reply_to(message, "❌ Неверный формат! Нужно: <code>Название|ip:port:user:pass</code>")
                    return
                p_name, p_proxy = raw_val.split("|", 1)
                p_name = p_name.strip()
                p_proxy = p_proxy.strip()
                save_proxy_to_file(p_name, p_proxy)
                self.bot.reply_to(message, f"✅ Прокси <b>{p_name}</b> добавлен в общий список!")

    def start(self):
        if self.running:
            return
        self.running = True

        def _poll():
            while self.running:
                try:
                    self.bot.infinity_polling(timeout=20, long_polling_timeout=20)
                except Exception as e:
                    time.sleep(3)

        self.thread = threading.Thread(target=_poll, daemon=True, name="TelegramBotPoll")
        self.thread.start()
        print("[+] Telegram бот управления успешно запущен!")

    def stop(self):
        self.running = False
        try:
            self.bot.stop_polling()
        except Exception:
            pass
