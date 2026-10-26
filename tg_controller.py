import threading
import time
import math
import os
import re
import telebot
from telebot import types
from ai_controller import AICommander, load_ai_config, save_ai_config

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

# Сколько узлов на одной странице раздела «Прокси и VPN». Раньше список
# обрезался, и до дальних узлов было не добраться.
VPN_PAGE_SIZE = 8


class TelegramBotController:
    def __init__(self, token: str, manager, admin_id=None):
        self.token = token.strip()
        self.manager = manager
        # Админов может быть несколько: один человек с телефона, второй с
        # ноутбука — оба должны управлять. Принимаем и одиночный id, и
        # список, и строку через запятую: раньше принимался только один id,
        # и второй админ молча получал «Доступ запрещен».
        self.admin_ids = self._parse_admins(admin_id)
        # Оставлено для совместимости: часть кода и внешние скрипты читают
        # admin_id как «главный админ».
        self.admin_id = self.admin_ids[0] if self.admin_ids else None
        self.bot = telebot.TeleBot(self.token, parse_mode="HTML")
        self.user_states = {}  # {chat_id: {"action": "...", "data": {...}}}
        self.ai = AICommander(self.manager)
        self.active_chats = set(self.admin_ids)
        self.thread = None
        self.running = False
        # Живые окна: (chat_id, message_id) -> что показываем и как выглядело.
        self._live_views = {}
        self._views_lock = threading.RLock()
        self._live_thread = None
        # Короткий номер -> ключ узла для кнопок «Прокси и VPN».
        self._vpn_picks = {}
        self._setup_handlers()

    @staticmethod
    def _parse_admins(admin_id) -> list[int]:
        """Привести аргумент к списку id, отбросив мусор."""
        if admin_id is None:
            return []
        if isinstance(admin_id, (int, str)):
            raw = [admin_id]
        else:
            raw = list(admin_id)
        out = []
        for item in raw:
            for piece in str(item).replace(";", ",").replace(" ", ",").split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    value = int(piece)
                except ValueError:
                    continue
                if value > 0 and value not in out:
                    out.append(value)
        return out

    def broadcast(self, text: str):
        for cid in list(self.active_chats):
            try:
                self.bot.send_message(cid, text)
            except Exception:
                pass

    def _is_authorized(self, user_id: int) -> bool:
        if not self.admin_ids:
            return True
        return user_id in self.admin_ids

    def _format_status_badge(self, status: str) -> str:
        s = status.lower()
        if "забанен" in s:
            return "🔴 БАН"
        elif "в игре" in s or "заспавнен" in s or "авторизован" in s:
            return "🟢 Онлайн"
        elif "очеред" in s or "ожидани" in s or "сессия занята" in s or "повтор" in s:
            return "🟢 Ожидание"
        elif "подключение" in s or "реконнект" in s:
            return "🟢 Коннект..."
        elif "остановлен" in s or "отключен" in s or "ошибка" in s or "кикнут" in s:
            return "🔴 Остановлен"
        return f"🔴 {status}"

    def _get_bot_button_style(self, status: str) -> str:
        s = status.lower()
        if (
            "в игре" in s
            or "заспавнен" in s
            or "авторизован" in s
            or "очеред" in s
            or "ожидани" in s
            or "сессия занята" in s
            or "повтор" in s
            or "подключение" in s
            or "реконнект" in s
        ):
            return "success"
        return "danger"

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
            btn_style = self._get_bot_button_style(b.status)
            bot_buttons.append(
                types.InlineKeyboardButton(
                    btn_text,
                    callback_data=f"bot_view:{bid}:{page}",
                    style=btn_style,
                )
            )
        
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
                nav_buttons.append(types.InlineKeyboardButton("⏪ -3", callback_data=f"page:{max(1, page-3)}", style="primary"))
            nav_buttons.append(types.InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}", style="primary"))

        nav_buttons.append(types.InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data=f"refresh:{page}", style="primary"))

        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton("➡️", callback_data=f"page:{page+1}", style="primary"))
            if page + 3 <= total_pages:
                nav_buttons.append(types.InlineKeyboardButton("+3 ⏩", callback_data=f"page:{min(total_pages, page+3)}", style="primary"))

        markup.row(*nav_buttons)

        # Быстрые глобальные действия
        markup.row(
            types.InlineKeyboardButton("▶️ Запустить выбранных", callback_data=f"act_start_some:{page}", style="success"),
            types.InlineKeyboardButton("⏹ Остановить выбранных", callback_data=f"act_stop_some:{page}", style="danger"),
        )
        markup.row(
            types.InlineKeyboardButton("🌐 Прокси выбранным", callback_data=f"act_proxy_some:{page}", style="primary"),
            types.InlineKeyboardButton("🎯 Цель выбранным", callback_data=f"act_goal_some:{page}", style="primary"),
        )
        markup.row(
            types.InlineKeyboardButton("🗑 Удалить выбранных", callback_data=f"act_del_some:{page}", style="danger"),
            types.InlineKeyboardButton("▶️ Всех", callback_data="act_startall", style="success"),
            types.InlineKeyboardButton("⏹ Всех", callback_data="act_stopall", style="danger")
        )
        markup.row(
            types.InlineKeyboardButton("🧠 ИИ Командир", callback_data="ai_menu", style="primary"),
            types.InlineKeyboardButton("🛡 Проверить баны", callback_data="act_check_bans", style="primary")
        )
        markup.row(
            types.InlineKeyboardButton("➕ Массовое создание", callback_data="act_batch", style="success"),
            types.InlineKeyboardButton("🌐 Прокси и VPN", callback_data="vpn_menu", style="primary")
        )
        markup.row(
            types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu", style="primary")
        )
        markup.row(
            types.InlineKeyboardButton("🔄 Обновить список", callback_data=f"page:{page}", style="primary")
        )
        return markup, total_bots, total_pages

    # ---------- настройки ----------

    def _render_settings(self, chat_id, message_id=None, callback_id=None):
        settings = getattr(self.manager, "settings", None)
        markup = types.InlineKeyboardMarkup(row_width=1)
        if settings is None:
            text = "<b>⚙️ Настройки</b>\n\n<i>Хранилище настроек не поднялось.</i>"
        else:
            pwd = settings.base_password or "<i>не задан</i>"
            text = (
                f"<b>⚙️ Настройки бота</b>\n\n"
                f"🔑 Базовый пароль: <code>{pwd}</code>\n"
                f"<i>Его получают все новые аккаунты: при массовой регистрации "
                f"и при автозамене забаненных.</i>\n\n"
                f"🎯 Задача по умолчанию: <code>{settings.default_task}</code>\n"
                f"📦 Лимит партии: <b>{settings.batch_limit}</b>\n"
                f"🌐 Прокси при автозамене: <b>{self._replace_proxy_label(settings)}</b>\n"
                f"<i>Какой адрес получит новый аккаунт, когда старый уйдёт "
                f"в бан и его заменит свежий.</i>"
            )
            markup.add(types.InlineKeyboardButton(
                "🔑 Изменить базовый пароль",
                callback_data="set_base_password", style="success"))
            markup.add(types.InlineKeyboardButton(
                f"🎯 Задача по умолчанию: {settings.default_task}",
                callback_data="set_default_task", style="primary"))
            markup.add(types.InlineKeyboardButton(
                f"📦 Лимит партии: {settings.batch_limit}",
                callback_data="set_batch_limit", style="primary"))
            markup.add(types.InlineKeyboardButton(
                f"🌐 Прокси при автозамене: {settings.replace_proxy}",
                callback_data="set_replace_proxy", style="primary"))
        markup.add(types.InlineKeyboardButton(
            "⬅️ Назад в меню", callback_data="page:1", style="primary"))

        if message_id is None:
            self.bot.send_message(chat_id, text, reply_markup=markup)
        else:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except Exception:
                self.bot.send_message(chat_id, text, reply_markup=markup)
        if callback_id:
            try:
                self.bot.answer_callback_query(callback_id)
            except Exception:
                pass

    REPLACE_PROXY_CHOICES = (
        ("inherit", "тот же, что был у старого"),
        ("pool", "взять из пула"),
        ("none", "своё IP"),
    )

    def _replace_proxy_label(self, settings) -> str:
        mode = settings.replace_proxy
        for key, title in self.REPLACE_PROXY_CHOICES:
            if key == mode:
                return f"{key} — {title}"
        return mode

    def _render_replace_proxy(self, chat_id, message_id=None, callback_id=None):
        settings = getattr(self.manager, "settings", None)
        if settings is None:
            return
        markup = types.InlineKeyboardMarkup(row_width=1)
        current = settings.replace_proxy
        for key, title in self.REPLACE_PROXY_CHOICES:
            mark = "✅ " if key == current else ""
            markup.add(types.InlineKeyboardButton(
                f"{mark}{title} ({key})",
                callback_data=f"replace_proxy:{key}",
                style="success" if key == current else "primary"))
        # Сохранённые прокси тоже годятся: у части людей адрес привязан
        # к аккаунту, и менять его при замене нельзя.
        for index, (name, _value) in enumerate(load_proxy_list()[:5]):
            mark = "✅ " if current == name else ""
            markup.add(types.InlineKeyboardButton(
                f"{mark}⚡️ {name}",
                callback_data=f"replace_proxy:s{index}",
                style="success" if current == name else "primary"))
        markup.add(types.InlineKeyboardButton(
            "⬅️ Назад", callback_data="settings", style="primary"))

        text = (
            "<b>🌐 Прокси при автозамене аккаунта</b>\n\n"
            "Когда аккаунт уходит в бан и вместо него создаётся новый, "
            "какой адрес ему дать.\n\n"
            "<b>inherit</b> — тот же, что был у старого. Новый аккаунт "
            "не светит свежий IP рядом со старым.\n"
            "<b>pool</b> — рабочий узел из пула.\n"
            "<b>none</b> — своё IP.\n\n"
            f"Сейчас: <code>{settings.replace_proxy}</code>"
        )
        if message_id is None:
            self.bot.send_message(chat_id, text, reply_markup=markup)
        else:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except Exception:
                self.bot.send_message(chat_id, text, reply_markup=markup)
        if callback_id:
            try:
                self.bot.answer_callback_query(callback_id)
            except Exception:
                pass

    # ---------- мастер массовой регистрации ----------
    #
    # Раньше всё вводилось одной строкой: «TVER 5 пароль lvl». С телефона
    # это мучение — надо помнить порядок, знать точное имя сервера и не
    # ошибиться в пароле. Теперь шаги: сервер (кнопками, с пагинацией) ->
    # количество -> пароль -> задача.

    SERVERS_PER_PAGE = 12

    def _batch_servers(self):
        try:
            import br_servers

            items = br_servers.get_servers()
        except Exception:
            return []
        result = []
        for srv in items:
            name = (srv.get("firstname") or srv.get("name") or "").strip()
            if not name:
                continue
            try:
                online = int(srv.get("online") or 0)
                maxonline = int(srv.get("maxonline") or 0)
            except (TypeError, ValueError):
                online = maxonline = 0
            result.append((name.upper(), online, maxonline))
        return result

    def _render_batch_servers(self, chat_id, page=1, message_id=None, callback_id=None):
        servers = self._batch_servers()
        per = self.SERVERS_PER_PAGE
        total_pages = max(1, math.ceil(len(servers) / per))
        page = max(1, min(int(page), total_pages))
        start = (page - 1) * per
        chunk = servers[start:start + per]

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        for name, online, maxonline in chunk:
            # Цвет по загруженности: свободно — зелёная, полный — красная.
            if maxonline and online >= maxonline * 0.95:
                style, mark = "danger", "🔴"
            elif maxonline and online >= maxonline * 0.75:
                style, mark = "primary", "🟡"
            else:
                style, mark = "success", "🟢"
            load = f"{online}/{maxonline}" if maxonline else "?"
            buttons.append(types.InlineKeyboardButton(
                f"{mark} {name} · {load}",
                callback_data=f"batch_srv:{name}",
                style=style,
            ))
        for i in range(0, len(buttons), 2):
            if i + 1 < len(buttons):
                markup.row(buttons[i], buttons[i + 1])
            else:
                markup.row(buttons[i])

        nav = []
        if page > 1:
            nav.append(types.InlineKeyboardButton(
                "⬅️", callback_data=f"batch_page:{page - 1}", style="primary"))
        nav.append(types.InlineKeyboardButton(
            f"📄 {page}/{total_pages}", callback_data=f"batch_page:{page}", style="primary"))
        if page < total_pages:
            nav.append(types.InlineKeyboardButton(
                "➡️", callback_data=f"batch_page:{page + 1}", style="primary"))
        markup.row(*nav)
        markup.row(types.InlineKeyboardButton(
            "✖️ Отмена", callback_data="batch_cancel", style="danger"))

        text = (
            f"<b>➕ Массовая регистрация — шаг 1 из 5</b>\n\n"
            f"Выбери сервер. Всего серверов: <b>{len(servers)}</b>, "
            f"страница <b>{page}</b> из <b>{total_pages}</b>.\n\n"
            f"🟢 свободно · 🟡 загружен · 🔴 почти полный"
        )
        if message_id is None:
            self.bot.send_message(chat_id, text, reply_markup=markup)
        else:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            except Exception:
                self.bot.send_message(chat_id, text, reply_markup=markup)
        if callback_id:
            try:
                self.bot.answer_callback_query(callback_id)
            except Exception:
                pass

    def _ask_batch_count(self, chat_id, server):
        settings = getattr(self.manager, "settings", None)
        limit = settings.batch_limit if settings else 50
        self.user_states[chat_id] = {
            "action": "batch_count",
            "server": server,
            "message_id": None,
        }
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton(
            "✖️ Отмена", callback_data="batch_cancel", style="danger"))
        sent = self.bot.send_message(
            chat_id,
            f"<b>➕ Массовая регистрация — шаг 2 из 5</b>\n\n"
            f"Сервер: <b>{server}</b>\n\n"
            f"Сколько аккаунтов создать? Пришли числом от 1 до {limit}.\n"
            f"<i>Например: 5</i>",
            reply_markup=markup,
        )
        self.user_states[chat_id]["message_id"] = getattr(sent, "message_id", None)

    def _ask_batch_password(self, chat_id, server, count):
        settings = getattr(self.manager, "settings", None)
        base = settings.base_password if settings else ""
        self.user_states[chat_id] = {
            "action": "batch_password",
            "server": server,
            "count": count,
        }
        markup = types.InlineKeyboardMarkup(row_width=1)
        if base:
            markup.add(types.InlineKeyboardButton(
                f"🔑 Базовый пароль ({base})",
                callback_data="batch_pwd_base", style="success"))
        markup.add(types.InlineKeyboardButton(
            "✖️ Отмена", callback_data="batch_cancel", style="danger"))
        hint = (
            f"Можно ничего не вводить и нажать кнопку с базовым паролем."
            if base else
            "Базовый пароль не задан — введи его здесь или задай в ⚙️ Настройках."
        )
        self.bot.send_message(
            chat_id,
            f"<b>➕ Массовая регистрация — шаг 3 из 5</b>\n\n"
            f"Сервер: <b>{server}</b> · аккаунтов: <b>{count}</b>\n\n"
            f"Какой пароль поставить новым аккаунтам?\n{hint}",
            reply_markup=markup,
        )

    def _ask_batch_task(self, chat_id, server, count, password):
        settings = getattr(self.manager, "settings", None)
        default = settings.default_task if settings else "lvl"
        self.user_states[chat_id] = {
            "action": "batch_task",
            "server": server,
            "count": count,
            "password": password,
        }
        markup = types.InlineKeyboardMarkup(row_width=2)
        labels = {
            "lvl": "📈 Прокачка",
            "quest": "📜 Квесты",
            "mine": "⛏ Шахта",
            "factory": "🏭 Завод",
            "bus": "🚌 Автобус",
            "farm": "🍎 Ферма",
            "stop": "⏸ Без задачи",
        }
        buttons = []
        for task, label in labels.items():
            style = "success" if task == default else "primary"
            buttons.append(types.InlineKeyboardButton(
                label, callback_data=f"batch_task:{task}", style=style))
        for i in range(0, len(buttons), 2):
            if i + 1 < len(buttons):
                markup.row(buttons[i], buttons[i + 1])
            else:
                markup.row(buttons[i])
        markup.row(types.InlineKeyboardButton(
            "✖️ Отмена", callback_data="batch_cancel", style="danger"))
        self.bot.send_message(
            chat_id,
            f"<b>➕ Массовая регистрация — шаг 4 из 5</b>\n\n"
            f"Сервер: <b>{server}</b> · аккаунтов: <b>{count}</b> · "
            f"пароль: <code>{password}</code>\n\n"
            f"Что делают аккаунты после входа?",
            reply_markup=markup,
        )

    def _ask_batch_proxy(self, chat_id, server, count, password, task):
        """Шаг 5: какой прокси дать новым аккаунтам.

        Раньше прокси в массовом создании не выбирался вообще: аккаунты
        поднимались с прямого IP, и десяток свежих ников светился одним
        адресом. Здесь либо конкретный узел, либо раздача рабочих по
        кругу из пула.
        """
        self.user_states[chat_id] = {
            "action": "batch_proxy",
            "server": server,
            "count": count,
            "password": password,
            "task": task,
        }
        pool = getattr(self.manager, "proxy_pool", None)
        markup = types.InlineKeyboardMarkup(row_width=1)
        picks = {}

        markup.add(types.InlineKeyboardButton(
            "🎲 Рабочие из пула (вразброс)",
            callback_data="batch_px:random", style="success"))
        markup.add(types.InlineKeyboardButton(
            "🚫 Своё IP (без прокси)",
            callback_data="batch_px:none", style="primary"))

        saved = load_proxy_list()
        for index, (name, _value) in enumerate(saved[:4]):
            token = f"s{index}"
            picks[token] = ("saved", name)
            markup.add(types.InlineKeyboardButton(
                f"⚡️ {name}", callback_data=f"batch_px:{token}", style="primary"))

        if pool is not None:
            for index, res in enumerate(pool.statuses()[:4]):
                node = res.node
                mark = "🟢" if res.usable else ("🟡" if res.alive else "🔴")
                label = f"{mark} {node.name[:20]}"
                if not node.is_direct:
                    label += " (мост)"
                token = f"n{index}"
                picks[token] = ("node", node.key)
                markup.add(types.InlineKeyboardButton(
                    label, callback_data=f"batch_px:{token}", style="primary"))

        self._batch_px_picks = picks
        markup.row(types.InlineKeyboardButton(
            "✖️ Отмена", callback_data="batch_cancel", style="danger"))

        self.bot.send_message(
            chat_id,
            f"<b>➕ Массовая регистрация — шаг 5 из 5</b>\n\n"
            f"Сервер: <b>{server}</b> · аккаунтов: <b>{count}</b> · "
            f"задача: <code>{task}</code>\n\n"
            f"Какой прокси дать новым аккаунтам?\n"
            f"<i>«Вразброс» раздаст рабочие узлы по кругу — по несколько "
            f"аккаунтов на узел, как при обычном запуске.</i>",
            reply_markup=markup,
        )

    def _batch_proxy_list(self, choice: str, count: int, picks: dict):
        """Собрать список прокси под партию. None — узел не поднялся."""
        pool = getattr(self.manager, "proxy_pool", None)

        if choice == "none":
            return []

        if choice == "random":
            if pool is None:
                return []
            try:
                spread = pool.rotate(count)
            except Exception:
                return []
            return [item for item in spread if item]

        pick = picks.get(choice)
        if pick is None:
            return None
        kind, value = pick

        if kind == "saved":
            url = dict(load_proxy_list()).get(value, "")
            return [url] * count if url else None

        node = pool.find_node(value) if pool else None
        if node is None:
            return None
        url = pool.socks_url_for(node) or ""
        if not url:
            return None
        return [url] * count

    def _run_batch(self, chat_id, server, count, password, task, proxies=None):
        settings = getattr(self.manager, "settings", None)
        limit = settings.batch_limit if settings else 50
        if count > limit:
            self.bot.send_message(
                chat_id,
                f"⚠️ Лимит партии — {limit}. Уменьши количество или подними "
                f"лимит в ⚙️ Настройках.")
            return
        status = self.bot.send_message(
            chat_id,
            f"⏳ Создаю <b>{count}</b> аккаунтов на <b>{server}</b> "
            f"(задача <code>{task}</code>)...\n"
            f"Это займёт примерно {count * 2} секунд."
        )

        def _worker():
            try:
                created = self.manager.add_bots_batch(
                    server, count, password=password, default_task=task,
                    proxies=proxies or None)
            except Exception as exc:
                self.bot.send_message(chat_id, f"❌ Ошибка: <code>{exc}</code>")
                return
            ok = [b for b in created if getattr(b, "nickname", None)]
            names = ", ".join(b.nickname for b in ok[:12])
            tail = f" и ещё {len(ok) - 12}" if len(ok) > 12 else ""
            with_proxy = len([b for b in ok if getattr(b, "proxy", "")])
            proxy_note = (
                f"\n🌐 С прокси: <b>{with_proxy}</b> из {len(ok)}"
                if with_proxy else "\n🌐 Прокси не назначался"
            )
            self.bot.send_message(
                chat_id,
                f"✅ Создано <b>{len(ok)}</b> из {count} на <b>{server}</b> "
                f"с задачей <code>{task}</code>.\n"
                f"{names}{tail}{proxy_note}\n\n"
                f"Список и статусы — /start."
            )

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- живые окна ----------
    #
    # Раньше статус в Telegram обновлялся только по нажатию кнопки. Запустил
    # бота — и смотришь на «Ожидание», пока сам не вспомнишь про «Обновить».
    # Теперь окна запоминаются, а фоновая нить раз в пару секунд проверяет,
    # изменилось ли содержимое, и перерисовывает только изменившиеся.

    def _remember_view(self, chat_id, message_id, kind, *, bid=None, page=1):
        if not message_id:
            return
        with self._views_lock:
            self._live_views[(chat_id, message_id)] = {
                "kind": kind,
                "bid": bid,
                "page": page,
                "sig": self._view_signature(kind, bid, page),
            }

    def _forget_view(self, chat_id, message_id=None):
        with self._views_lock:
            if message_id is None:
                for key in [k for k in self._live_views if k[0] == chat_id]:
                    self._live_views.pop(key, None)
            else:
                self._live_views.pop((chat_id, message_id), None)

    def _view_signature(self, kind, bid, page):
        """Короткая подпись содержимого окна: меняется — перерисовываем."""
        if kind == "card":
            with self.manager._lock:
                b = self.manager.bots.get(bid)
            if b is None:
                return None
            return (
                b.status,
                str(b.get_level()),
                str(b.get_balance_str()),
                b.active_task.name if b.active_task else None,
                b.default_task_name,
                self.manager._get_bot_location_name(b),
                b.proxy,
                b.is_banned,
            )
        # Список: статусы всех ботов страницы плюс их состав.
        with self.manager._lock:
            items = sorted(self.manager.bots.items(), key=lambda x: x[0])
        total = len(items)
        total_pages = max(1, math.ceil(total / BOTS_PER_PAGE))
        page = max(1, min(page, total_pages))
        start = (page - 1) * BOTS_PER_PAGE
        chunk = items[start:start + BOTS_PER_PAGE]
        return (total, tuple((bid, b.status, b.nickname) for bid, b in chunk))

    PHASE_NAMES = {
        "world": "вход в мир",
        "delay": "ждёт минуту после спавна",
        "target": "выбирает точку",
        "moving": "идёт на точку",
        "command": "отправляет /bg",
        "done": "на точке",
    }

    def _point_line(self, b) -> str:
        """Строка про вокзал и точку качки для карточки бота."""
        station = getattr(b, "lvl_spawn_name", None)
        point = getattr(b, "lvl_target", None)
        index = getattr(b, "lvl_target_index", None)
        phase = getattr(b, "lvl_phase", None)
        if not station and not point:
            return ""
        parts = []
        if station:
            parts.append(f"🚉 Вокзал: <b>{station}</b>")
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            number = f"точка {index + 1} · " if isinstance(index, int) else ""
            coords = f"{float(point[0]):.1f}, {float(point[1]):.1f}"
            parts.append(f"\n🎯 {number}<code>{coords}</code>")
        if phase in self.PHASE_NAMES:
            parts.append(f"\n⏳ Этап: <i>{self.PHASE_NAMES[phase]}</i>")
        return "".join(f"\n{part}" if not part.startswith("\n") else part
                       for part in parts)

    def _parse_ids(self, raw: str) -> list[int]:
        """Разобрать «1-5, 8, 12-15» в список ID.

        Один разбор на запуск, остановку и прокси: три копии одинакового
        кода разъезжались, и диапазоны в одном месте работали, а в другом
        молча игнорировались.
        """
        ids = set()
        for token in re.split(r"[\s,]+", (raw or "").strip()):
            if not token:
                continue
            if "-" in token:
                left, _, right = token.partition("-")
                if left.isdigit() and right.isdigit():
                    start, end = int(left), int(right)
                    if start > end:
                        start, end = end, start
                    # Ограничение, чтобы опечатка «1-99999» не повесила бота.
                    for value in range(start, min(end, start + 500) + 1):
                        ids.add(value)
            elif token.isdigit():
                ids.add(int(token))
        return sorted(ids)

    def _ask_bulk_proxy(self, chat_id, ids: list[int], page: int = 1):
        """Меню выбора прокси для пачки аккаунтов."""
        self._bulk_proxy_ids = ids
        pool = getattr(self.manager, "proxy_pool", None)
        saved = load_proxy_list()
        markup = types.InlineKeyboardMarkup(row_width=1)
        picks = {}

        for index, (name, _value) in enumerate(saved[:6]):
            token = f"s{index}"
            picks[token] = ("saved", name)
            markup.add(types.InlineKeyboardButton(
                f"⚡️ {name}", callback_data=f"bulkpx:{token}", style="success"))

        if pool is not None:
            for index, res in enumerate(pool.statuses()[:6]):
                node = res.node
                mark = "🟢" if res.usable else ("🟡" if res.alive else "🔴")
                label = f"{mark} {node.name[:22]}"
                if not node.is_direct:
                    label += " (мост)"
                token = f"n{index}"
                picks[token] = ("node", node.key)
                markup.add(types.InlineKeyboardButton(
                    label, callback_data=f"bulkpx:{token}", style="primary"))

        self._bulk_px_picks = picks
        markup.add(types.InlineKeyboardButton(
            "🚫 Свой IP (сбросить)", callback_data="bulkpx:none", style="danger"))
        markup.add(types.InlineKeyboardButton(
            "⬅️ Отмена", callback_data=f"page:{page}", style="primary"))

        self.bot.send_message(
            chat_id,
            f"<b>🌐 Прокси для {len(ids)} аккаунт(ов)</b>\n\n"
            f"Выбери узел. Узлы с пометкой «мост» поднимаются через sing-box "
            f"и тоже рабочие.",
            reply_markup=markup,
        )

    def _bot_card(self, b, page):
        """Текст и кнопки карточки бота. Общие для показа и автообновления."""
        loc = self.manager._get_bot_location_name(b)
        loc_str = f"\n📍 Локация: <code>{loc}</code>" if loc else ""
        # Куда движок качки поставил бота. Вокзал спавна и точка, на
        # которую он ушёл: по карточке видно, кто где стоит, а не только
        # «в игре».
        point_str = self._point_line(b)
        proxy_str = f"\n🌐 Прокси: <code>{b.proxy}</code>" if b.proxy else ""
        task_str = b.active_task.name if b.active_task else (b.default_task_name or "нет")
        goal_str = (
            f"\n🎯 Цель: <b>{b.target_lvl} lvl</b>" if b.target_lvl
            else "\n🎯 Цель: <i>не задана</i>"
        )
        bid = b.bot_id

        text = (
            f"<b>👤 Информация о боте #{bid}</b>\n"
            f"Никнейм: <b>{b.nickname}</b>\n"
            f"Сервер: <b>{b.server_name}</b> ({b.host}:{b.port})\n"
            f"Статус: <b>{self._format_status_badge(b.status)}</b>\n"
            f"Уровень: <b>{b.get_level()}</b>\n"
            f"Баланс: <b>{b.get_balance_str()}</b>\n"
            f"Задача: <code>{task_str}</code>{goal_str}{point_str}{loc_str}{proxy_str}\n"
            f"Пароль: <code>{b.password}</code>"
        )

        markup = types.InlineKeyboardMarkup(row_width=2)
        s_lower = b.status.lower()
        is_active = (
            "в игре" in s_lower
            or "подключение" in s_lower
            or "очеред" in s_lower
            or "ожидани" in s_lower
            or "сессия занята" in s_lower
            or "повтор" in s_lower
            or b.queue_mode
            or getattr(b, "_reconnect_pending", False)
        )
        if is_active:
            btn = types.InlineKeyboardButton(
                "⏹ Остановить", callback_data=f"bot_stop:{bid}:{page}", style="danger")
        else:
            btn = types.InlineKeyboardButton(
                "▶️ Запустить", callback_data=f"bot_start:{bid}:{page}", style="success")

        markup.row(btn, types.InlineKeyboardButton(
            "📜 Логи", callback_data=f"bot_logs:{bid}:{page}", style="primary"))
        markup.row(
            types.InlineKeyboardButton("🔄 Обновить статы (/mm)",
                                       callback_data=f"bot_check_stats:{bid}:{page}", style="success"),
            types.InlineKeyboardButton("🎯 Задать задачу",
                                       callback_data=f"bot_tasks:{bid}:{page}", style="primary")
        )
        markup.row(
            types.InlineKeyboardButton("🏆 Задать цель (LVL)",
                                       callback_data=f"bot_set_goal:{bid}:{page}", style="primary"),
            types.InlineKeyboardButton("💬 Чат / Команда",
                                       callback_data=f"bot_chat:{bid}:{page}", style="primary")
        )
        markup.row(
            types.InlineKeyboardButton("🌐 Прокси",
                                       callback_data=f"bot_proxy_menu:{bid}:{page}", style="primary"),
            types.InlineKeyboardButton("❌ Удалить бота",
                                       callback_data=f"bot_del:{bid}:{page}", style="danger")
        )
        if b.is_banned:
            markup.row(types.InlineKeyboardButton(
                "🔓 Снять бан", callback_data=f"bot_unban:{bid}:{page}", style="success"))
        markup.row(types.InlineKeyboardButton(
            "⬅️ Назад к списку", callback_data=f"page:{page}", style="primary"))
        return text, markup

    def _refresh_live_views(self):
        """Перерисовать окна, содержимое которых изменилось."""
        with self._views_lock:
            snapshot = list(self._live_views.items())

        for (chat_id, message_id), info in snapshot:
            kind = info["kind"]
            bid = info["bid"]
            page = info["page"]
            try:
                sig = self._view_signature(kind, bid, page)
            except Exception:
                continue
            if sig is None:
                # Бота удалили — окно больше не о чем показывать.
                self._forget_view(chat_id, message_id)
                continue
            if sig == info["sig"]:
                continue

            try:
                if kind == "card":
                    with self.manager._lock:
                        b = self.manager.bots.get(bid)
                    if b is None:
                        self._forget_view(chat_id, message_id)
                        continue
                    text, markup = self._bot_card(b, page)
                else:
                    text, markup = self._list_text(page)
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
                with self._views_lock:
                    if (chat_id, message_id) in self._live_views:
                        self._live_views[(chat_id, message_id)]["sig"] = sig
            except Exception as exc:
                # «message is not modified» — норма, остальное значит, что
                # окно умерло (удалено, слишком старое). Не копим мусор.
                if "not modified" not in str(exc).lower():
                    self._forget_view(chat_id, message_id)

    def _list_text(self, page: int):
        """Текст и клавиатура страницы списка ботов."""
        markup, total, total_pages = self._build_pagination_keyboard(page)
        page = max(1, min(page, total_pages))
        with self.manager._lock:
            items = sorted(self.manager.bots.items(), key=lambda x: x[0])
        start = (page - 1) * BOTS_PER_PAGE
        chunk = items[start:start + BOTS_PER_PAGE]

        online = sum(1 for _, b in items if self._format_status_badge(b.status) == "🟢 Онлайн")
        banned = sum(1 for _, b in items if b.is_banned)

        lines = [
            f"<b>🤖 Панель управления ботами</b>",
            "",
            f"Всего: <b>{total}</b> · Онлайн: <b>{online}</b> · Бан: <b>{banned}</b>",
            f"Страница <b>{page}</b> из <b>{total_pages}</b>",
        ]
        return "\n".join(lines), markup

    def _start_live_refresh(self):
        """Фоновая нить автообновления окон."""
        if getattr(self, "_live_thread", None) is not None:
            return
        interval = float(os.environ.get("BR_LIVE_REFRESH", "3.0"))

        def _loop():
            while self.running:
                time.sleep(interval)
                if not self.running:
                    break
                try:
                    self._refresh_live_views()
                except Exception:
                    pass

        self._live_thread = threading.Thread(target=_loop, daemon=True)
        self._live_thread.start()

    def _render_vpn_menu(self, chat_id, message_id=None, callback_id=None,
                         quiet=False, page: int = 1):
        """Раздел «Прокси и VPN»: узлы цветом, добавление, проверка, раздача.

        Узлы листаются страницами. Раньше в тексте было 12 узлов, а
        кнопок 16 — остальные просто не показывались, и при пуле больше
        полутора десятков добраться до них было нельзя.
        """
        pool = getattr(self.manager, "proxy_pool", None)
        markup = types.InlineKeyboardMarkup(row_width=1)
        vpn_page = max(1, int(page or 1))

        if pool is None:
            text = "<b>🌐 Прокси и VPN</b>\n\n<i>Пул прокси не запущен.</i>"
        else:
            statuses = pool.statuses()
            summary = pool.summary()
            lines = ["<b>🌐 Прокси и VPN</b>", ""]

            if statuses:
                lines.append(
                    f"Рабочих: <b>{summary['usable']}</b> из {summary['total']} · "
                    f"проверка каждые {summary['interval']:.0f}с"
                )
                if summary["last_refresh"]:
                    stamp = time.strftime("%H:%M:%S", time.localtime(summary["last_refresh"]))
                    lines.append(f"Обновлено: {stamp}")
                lines.append("")
                pages = max(1, (len(statuses) + VPN_PAGE_SIZE - 1) // VPN_PAGE_SIZE)
                vpn_page = max(1, min(vpn_page, pages))
                start = (vpn_page - 1) * VPN_PAGE_SIZE
                window = statuses[start:start + VPN_PAGE_SIZE]

                for res in window:
                    node = res.node
                    if getattr(res, "status", "") == "banned":
                        mark = "⛔️"
                    elif res.usable:
                        mark = "🟢"
                    elif res.alive:
                        mark = "🟡"
                    else:
                        mark = "🔴"
                    latency = f" · {res.latency_ms:.0f}мс" if res.latency_ms else ""
                    note = f"\n     <i>{res.reason}</i>" if res.reason else ""
                    lines.append(
                        f"{mark} <b>{node.name}</b>\n"
                        f"     <code>{node.address}:{node.port}</code> "
                        f"[{node.protocol}]{latency}{note}"
                    )
                if pages > 1:
                    lines.append("")
                    lines.append(f"<i>Страница {vpn_page} из {pages}</i>")
                lines.append("")
                lines.append("🟢 рабочий · 🟡 нужен клиент · 🔴 мёртвый · ⛔️ забанен")
                # Без моста узлы vless/vmess/trojan/ss применить некуда:
                # бот умеет только SOCKS5. Говорим прямо, что сделать.
                bridge = getattr(pool, "bridge", None)
                if bridge is None or not getattr(bridge, "available", lambda: False)():
                    lines.append("")
                    lines.append(
                        "⚠️ <b>Мост sing-box не установлен.</b> Узлы 🟡 применить "
                        "нельзя — бот ходит только через SOCKS5.\n"
                        "На сервере: <code>bash deploy/install_singbox.sh</code>"
                    )
            else:
                lines.append("<i>Узлов нет. Добавь подписку или локальный прокси.</i>")

            if summary["last_error"]:
                lines.append(f"\n⚠️ {summary['last_error']}")

            text = "\n".join(lines)

            # Узлы кнопками: зелёная — рабочая, красная — мёртвая. По клику
            # раздаётся всем ботам. Раньше узлы были только текстом, и
            # выбрать конкретный сервер из Telegram было нельзя.
            if statuses:
                markup.add(types.InlineKeyboardButton(
                    "— Раздать узел всем ботам —", callback_data="vpn_noop", style="primary"))
                # callback_data ограничен 64 байтами. Ключ узла теперь
                # содержит UUID и отпечаток конфига — он длиннее лимита, и
                # Telegram отвергал всю клавиатуру с BUTTON_DATA_INVALID:
                # кнопка «Прокси и VPN» просто не открывалась.
                # В кнопку кладём короткий номер, а ключ держим рядом.
                self._vpn_picks = {}
                for index, res in enumerate(window):
                    node = res.node
                    if getattr(res, "status", "") == "banned":
                        mark, style = "⛔️", "danger"
                    elif res.usable:
                        mark, style = "🟢", "success"
                    elif res.alive:
                        mark, style = "🟡", "primary"
                    else:
                        mark, style = "🔴", "danger"
                    label = f"{mark} {node.name[:26]} · {node.protocol}"
                    token = str(index)
                    self._vpn_picks[token] = node.key
                    markup.add(types.InlineKeyboardButton(
                        label, callback_data=f"vpn_pick:{token}", style=style))

                # Листалка: по краям — в начало и в конец.
                if pages > 1:
                    nav = []
                    if vpn_page > 1:
                        nav.append(types.InlineKeyboardButton(
                            "«", callback_data="vpn_page:1", style="primary"))
                        nav.append(types.InlineKeyboardButton(
                            "‹", callback_data=f"vpn_page:{vpn_page - 1}",
                            style="primary"))
                    nav.append(types.InlineKeyboardButton(
                        f"{vpn_page}/{pages}", callback_data="vpn_noop",
                        style="primary"))
                    if vpn_page < pages:
                        nav.append(types.InlineKeyboardButton(
                            "›", callback_data=f"vpn_page:{vpn_page + 1}",
                            style="primary"))
                        nav.append(types.InlineKeyboardButton(
                            "»", callback_data=f"vpn_page:{pages}",
                            style="primary"))
                    markup.row(*nav)

            if summary["usable"]:
                markup.add(types.InlineKeyboardButton(
                    "⚡️ Раздать рабочие всем ботам", callback_data="vpn_apply_all", style="success"))
                markup.add(types.InlineKeyboardButton(
                    "🔀 Ротация по узлам (3 бота на узел)", callback_data="vpn_rotate", style="success"))
            markup.add(types.InlineKeyboardButton("🔄 Проверить сейчас", callback_data="vpn_refresh", style="success"))

        markup.add(types.InlineKeyboardButton("🔗 Добавить VPN-подписку", callback_data="vpn_add_sub", style="success"))
        markup.add(types.InlineKeyboardButton("🔌 Добавить локальный прокси", callback_data="vpn_add_local", style="success"))
        markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="page:1", style="primary"))

        if message_id is None:
            self.bot.send_message(chat_id, text, reply_markup=markup)
        else:
            try:
                self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                )
            except Exception:
                if not quiet:
                    self.bot.send_message(chat_id, text, reply_markup=markup)
        if callback_id:
            try:
                self.bot.answer_callback_query(callback_id)
            except Exception:
                pass

    def _setup_handlers(self):
        @self.bot.message_handler(commands=["start", "menu", "help"])
        def cmd_start(message):
            if not self._is_authorized(message.from_user.id):
                self.bot.reply_to(message, "⛔️ Доступ запрещен. Укажите ваш Telegram ID администратора.")
                return

            self.active_chats.add(message.chat.id)
            text, markup = self._list_text(1)
            sent = self.bot.send_message(message.chat.id, text, reply_markup=markup)
            # Главное окно тоже живое: статусы обновляются без /start.
            self._remember_view(message.chat.id, getattr(sent, "message_id", None), "list", page=1)

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
                text, markup = self._list_text(page)
                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                # Запоминаем окно: дальше оно обновляется само.
                self._remember_view(chat_id, message_id, "list", page=page)
                self.bot.answer_callback_query(call.id)

            elif data.startswith("refresh:"):
                page = int(data.split(":")[1])
                text, markup = self._list_text(page)
                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self._remember_view(chat_id, message_id, "list", page=page)
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

            elif data.startswith("act_stop_some:"):
                page = int(data.split(":")[1])
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "stop_selected_bots", "page": page}
                self.bot.send_message(
                    chat_id,
                    "<b>⏹ Остановка нескольких ботов</b>\n\n"
                    "Введите ID через пробел или запятую, либо диапазон:\n"
                    "• <code>1 2 5 8</code>\n"
                    "• <code>1-10</code>\n"
                    "• <code>1-5, 8, 12-15</code>\n\n"
                    "Для отмены: /cancel",
                )

            elif data.startswith("act_goal_some:"):
                page = int(data.split(":")[1])
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "goal_selected_bots", "page": page}
                self.bot.send_message(
                    chat_id,
                    "<b>🎯 Цель по уровню для нескольких ботов</b>\n\n"
                    "Сначала ID через пробел или диапазон:\n"
                    "• <code>1 2 5 8</code>\n"
                    "• <code>1-10</code>\n\n"
                    "Дальше пришлёшь уровень. Для отмены: /cancel",
                )

            elif data.startswith("act_del_some:"):
                page = int(data.split(":")[1])
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "del_selected_bots", "page": page}
                self.bot.send_message(
                    chat_id,
                    "<b>🗑 Удаление нескольких ботов</b>\n\n"
                    "Введите ID через пробел или диапазон:\n"
                    "• <code>1 2 5 8</code>\n"
                    "• <code>1-10</code>\n\n"
                    "Боты будут остановлены и удалены из реестра. "
                    "Для отмены: /cancel",
                )

            elif data == "del_some_confirm":
                ids = getattr(self, "_del_some_ids", None) or []
                self._del_some_ids = []
                if not ids:
                    self.bot.answer_callback_query(call.id, "Список устарел")
                    return
                removed = self.manager.delete_selected(ids)
                self.bot.answer_callback_query(call.id, f"Удалено: {len(removed)}")
                self.bot.send_message(
                    chat_id,
                    f"🗑 Удалено аккаунтов: <b>{len(removed)}</b>\n"
                    + (", ".join(f"#{i}" for i in removed[:20]) if removed else "—"),
                )

            elif data.startswith("act_proxy_some:"):
                page = int(data.split(":")[1])
                self.bot.answer_callback_query(call.id)
                self.user_states[chat_id] = {"action": "proxy_selected_bots", "page": page}
                self.bot.send_message(
                    chat_id,
                    "<b>🌐 Прокси для нескольких ботов</b>\n\n"
                    "Введите ID через пробел или запятую, либо диапазон:\n"
                    "• <code>1 2 5 8</code>\n"
                    "• <code>1-10</code>\n\n"
                    "Дальше выберете узел. Для отмены: /cancel",
                )

            elif data.startswith("bulkpx:"):
                token = data.split(":", 1)[1]
                ids = getattr(self, "_bulk_proxy_ids", None) or []
                if not ids:
                    self.bot.answer_callback_query(
                        call.id, "Список устарел, начни заново", show_alert=True)
                    return

                if token == "none":
                    # Токен, а не пустая строка: set_bot_proxy различает
                    # «сбросить» и «прокси не годится».
                    proxy = "none"
                else:
                    pick = (getattr(self, "_bulk_px_picks", None) or {}).get(token)
                    if pick is None:
                        self.bot.answer_callback_query(
                            call.id, "Меню устарело, открой заново", show_alert=True)
                        return
                    kind, value = pick
                    if kind == "saved":
                        proxy = dict(load_proxy_list()).get(value, "")
                    else:
                        pool = getattr(self.manager, "proxy_pool", None)
                        node = pool.find_node(value) if pool else None
                        if node is None:
                            self.bot.answer_callback_query(
                                call.id, "Узел не найден", show_alert=True)
                            return
                        proxy = pool.socks_url_for(node) or ""
                        if not proxy:
                            self.bot.answer_callback_query(
                                call.id,
                                f"{node.name}: не поднялся мост sing-box",
                                show_alert=True,
                            )
                            return

                done = 0
                for bot_id in ids:
                    try:
                        if self.manager.set_bot_proxy(bot_id, proxy):
                            done += 1
                    except Exception:
                        continue
                self.bot.answer_callback_query(
                    call.id, f"Прокси применён: {done} из {len(ids)}", show_alert=True)
                self._bulk_proxy_ids = []

            elif data == "act_startall":
                self.bot.answer_callback_query(call.id, "Запуск всех ботов...")
                threading.Thread(target=self.manager.start_all, daemon=True).start()
                self.bot.send_message(chat_id, "🚀 Подан сигнал на запуск всех доступных ботов!")

            elif data == "act_stopall":
                # Раньше здесь сразу шёл stop_all(): один случайный тап по
                # кнопке гасил весь ботнет, отменить было нельзя.
                with self.manager._lock:
                    all_bots = list(self.manager.bots.values())
                active = [b for b in all_bots if not b.is_banned]
                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    chat_id,
                    f"⚠️ <b>Остановить всех ботов?</b>\n\n"
                    f"Всего аккаунтов: <b>{len(all_bots)}</b>\n"
                    f"Из них активных: <b>{len(active)}</b>\n\n"
                    f"Все сессии будут закрыты. Заново запускать вручную.",
                    reply_markup=types.InlineKeyboardMarkup().row(
                        types.InlineKeyboardButton(
                            f"⏹ Да, остановить {len(active)}",
                            callback_data="act_stopall_confirm", style="danger"),
                        types.InlineKeyboardButton(
                            "↩️ Отмена", callback_data="act_stopall_cancel",
                            style="primary"),
                    ),
                )

            elif data == "act_stopall_confirm":
                self.bot.answer_callback_query(call.id, "Остановка ботов...")
                self.manager.stop_all()
                self.bot.send_message(chat_id, "⏹ Все боты остановлены.")

            elif data == "act_stopall_cancel":
                self.bot.answer_callback_query(call.id, "Отменено")
                text, markup = self._list_text(1)
                self.bot.send_message(chat_id, "Отменено. " + text, reply_markup=markup)

            elif data == "act_batch":
                self.bot.answer_callback_query(call.id)
                self._render_batch_servers(chat_id, 1)

            elif data.startswith("batch_page:"):
                page = int(data.split(":", 1)[1])
                self._render_batch_servers(chat_id, page, message_id, call.id)

            elif data.startswith("batch_srv:"):
                server = data.split(":", 1)[1]
                self.bot.answer_callback_query(call.id, f"Сервер {server}")
                self._ask_batch_count(chat_id, server)

            elif data == "batch_pwd_base":
                state = self.user_states.get(chat_id) or {}
                if state.get("action") != "batch_password":
                    self.bot.answer_callback_query(call.id, "Шаг устарел, начни заново")
                    return
                settings = getattr(self.manager, "settings", None)
                password = settings.base_password if settings else ""
                if not password:
                    self.bot.answer_callback_query(
                        call.id, "Базовый пароль не задан", show_alert=True)
                    return
                self.bot.answer_callback_query(call.id, "Беру базовый пароль")
                self._ask_batch_task(
                    chat_id, state["server"], state["count"], password)

            elif data.startswith("batch_task:"):
                task = data.split(":", 1)[1]
                state = self.user_states.get(chat_id) or {}
                if state.get("action") != "batch_task":
                    self.bot.answer_callback_query(call.id, "Шаг устарел, начни заново")
                    return
                self.bot.answer_callback_query(call.id, f"Задача {task}")
                self._ask_batch_proxy(
                    chat_id, state["server"], state["count"],
                    state["password"], task)

            elif data.startswith("batch_px:"):
                choice = data.split(":", 1)[1]
                state = self.user_states.get(chat_id) or {}
                if state.get("action") != "batch_proxy":
                    self.bot.answer_callback_query(call.id, "Шаг устарел, начни заново")
                    return
                del self.user_states[chat_id]
                proxies = self._batch_proxy_list(
                    choice, state["count"], getattr(self, "_batch_px_picks", None) or {})
                if proxies is None:
                    self.bot.answer_callback_query(
                        call.id, "Узел не поднялся — выбери другой", show_alert=True)
                    return
                self.bot.answer_callback_query(call.id, "Создаю")
                self._run_batch(
                    chat_id, state["server"], state["count"],
                    state["password"], state["task"], proxies)

            elif data == "batch_cancel":
                self.user_states.pop(chat_id, None)
                self.bot.answer_callback_query(call.id, "Отменено")
                try:
                    self.bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
                text, markup = self._list_text(1)
                sent = self.bot.send_message(chat_id, text, reply_markup=markup)
                self._remember_view(
                    chat_id, getattr(sent, "message_id", None), "list", page=1)

            elif data == "settings_menu":
                self._render_settings(chat_id, message_id, call.id)

            elif data == "settings":
                self._render_settings(chat_id, message_id, call.id)

            elif data == "set_replace_proxy":
                self._render_replace_proxy(chat_id, message_id, call.id)

            elif data.startswith("replace_proxy:"):
                choice = data.split(":", 1)[1]
                settings = getattr(self.manager, "settings", None)
                if settings is None:
                    self.bot.answer_callback_query(call.id, "Настройки недоступны")
                    return
                if choice.startswith("s") and choice[1:].isdigit():
                    saved = load_proxy_list()
                    index = int(choice[1:])
                    if index >= len(saved):
                        self.bot.answer_callback_query(
                            call.id, "Список устарел", show_alert=True)
                        return
                    settings.replace_proxy = saved[index][0]
                else:
                    settings.replace_proxy = choice
                self._render_replace_proxy(chat_id, message_id, call.id)

            elif data == "set_base_password":
                self.user_states[chat_id] = {"action": "set_base_password"}
                self.bot.send_message(
                    chat_id,
                    "<b>🔑 Базовый пароль</b>\n\n"
                    "Пришли пароль, который будут получать новые аккаунты.\n"
                    "Минимум 5 символов.\n\n"
                    "Для отмены: /cancel",
                )
                self.bot.answer_callback_query(call.id)

            elif data == "set_default_task":
                settings = getattr(self.manager, "settings", None)
                if settings is None:
                    self.bot.answer_callback_query(call.id, "Настройки недоступны")
                    return
                self.user_states.pop(chat_id, None)
                markup = types.InlineKeyboardMarkup(row_width=2)
                labels = {
                    "lvl": "📈 Прокачка", "quest": "📜 Квесты",
                    "mine": "⛏ Шахта", "factory": "🏭 Завод",
                    "bus": "🚌 Автобус", "farm": "🍎 Ферма",
                    "stop": "⏸ Без задачи",
                }
                buttons = [
                    types.InlineKeyboardButton(
                        label,
                        callback_data=f"pick_default_task:{task}",
                        style="success" if task == settings.default_task else "primary",
                    )
                    for task, label in labels.items()
                ]
                for i in range(0, len(buttons), 2):
                    if i + 1 < len(buttons):
                        markup.row(buttons[i], buttons[i + 1])
                    else:
                        markup.row(buttons[i])
                self.bot.send_message(
                    chat_id,
                    f"<b>🎯 Задача по умолчанию</b>\n\n"
                    f"Сейчас: <code>{settings.default_task}</code>.\n"
                    f"Её получат новые аккаунты, если не выбрать другую.",
                    reply_markup=markup,
                )
                self.bot.answer_callback_query(call.id)

            elif data.startswith("pick_default_task:"):
                task = data.split(":", 1)[1]
                settings = getattr(self.manager, "settings", None)
                if settings is None:
                    self.bot.answer_callback_query(call.id, "Настройки недоступны")
                    return
                settings.default_task = task
                self.user_states.pop(chat_id, None)
                self.bot.answer_callback_query(call.id, f"Задача: {task}")
                self._render_settings(chat_id, message_id, call.id)

            elif data == "set_batch_limit":
                self.user_states[chat_id] = {"action": "set_batch_limit"}
                self.bot.send_message(
                    chat_id,
                    "<b>📦 Лимит партии</b>\n\n"
                    "Сколько аккаунтов разрешено создавать за один заход?\n"
                    "Пришли число от 1 до 500.\n\n"
                    "Для отмены: /cancel",
                )
                self.bot.answer_callback_query(call.id)

            elif data.startswith("bot_view:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    b = self.manager.bots.get(bid)

                if not b:
                    self.bot.answer_callback_query(call.id, "Бот не найден!", show_alert=True)
                    return

                text, markup = self._bot_card(b, page)
                try:
                    self.bot.edit_message_text(
                        text, chat_id=chat_id, message_id=message_id, reply_markup=markup
                    )
                except Exception:
                    pass
                self._remember_view(chat_id, message_id, "card", bid=bid, page=page)
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
                # Удаление необратимо: аккаунт, пароль и статистика исчезают
                # из реестра. Без подтверждения один промах по кнопке стирал
                # аккаунт молча.
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                with self.manager._lock:
                    target = self.manager.bots.get(bid)
                if target is None:
                    self.bot.answer_callback_query(call.id, "Бот уже удалён")
                    return
                self.bot.answer_callback_query(call.id)
                self.bot.send_message(
                    chat_id,
                    f"⚠️ <b>Удалить бота #{bid}?</b>\n\n"
                    f"Ник: <code>{target.nickname}</code>\n"
                    f"Сервер: <b>{target.server_name}</b>\n"
                    f"Уровень: <b>{target.get_level()}</b>\n\n"
                    f"Аккаунт исчезнет из реестра вместе с паролем и "
                    f"статистикой. Отменить это нельзя.",
                    reply_markup=types.InlineKeyboardMarkup().row(
                        types.InlineKeyboardButton(
                            "❌ Да, удалить",
                            callback_data=f"bot_del_confirm:{bid}:{page}",
                            style="danger"),
                        types.InlineKeyboardButton(
                            "↩️ Отмена",
                            callback_data=f"bot_view:{bid}:{page}",
                            style="primary"),
                    ),
                )

            elif data.startswith("bot_del_confirm:"):
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
                    types.InlineKeyboardButton("🔄 Обновить логи", callback_data=f"bot_logs:{bid}:{page}", style="primary"),
                    types.InlineKeyboardButton("⬅️ Назад к боту", callback_data=f"bot_view:{bid}:{page}", style="primary")
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
                    types.InlineKeyboardButton("Качать LVL (АФК)", callback_data=f"set_task:{bid}:{page}:lvl", style="success"),
                    types.InlineKeyboardButton("Квесты", callback_data=f"set_task:{bid}:{page}:quest", style="success")
                )
                markup.row(
                    types.InlineKeyboardButton("Шахта (Miner)", callback_data=f"set_task:{bid}:{page}:mine", style="success"),
                    types.InlineKeyboardButton("Завод (Factory)", callback_data=f"set_task:{bid}:{page}:factory", style="success")
                )
                markup.row(
                    types.InlineKeyboardButton("Автобусник", callback_data=f"set_task:{bid}:{page}:bus", style="success"),
                    types.InlineKeyboardButton("⏹ Снять задачу", callback_data=f"set_task:{bid}:{page}:stop", style="danger")
                )
                markup.row(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"bot_view:{bid}:{page}", style="primary"))
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
                    markup.add(types.InlineKeyboardButton(f"⚡️ {name}", callback_data=f"apply_px:{bid}:{page}:{name}", style="success"))

                # Узлы из пула: зелёные рабочие, жёлтые живые без клиента,
                # красные мёртвые. Нажать можно только зелёный.
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is not None:
                    # Полный ключ узла в callback_data не влезает (лимит
                    # 64 байта), поэтому в кнопку кладём короткий номер,
                    # а сам ключ держим в словаре на этот рендер.
                    picks = {}
                    for index, res in enumerate(pool.statuses()[:8]):
                        node = res.node
                        if res.usable:
                            mark = "🟢"
                        elif res.alive:
                            mark = "🟡"
                        else:
                            mark = "🔴"
                        latency = f" {res.latency_ms:.0f}мс" if res.latency_ms else ""
                        label = f"{mark} {node.name[:22]}{latency}"
                        # Узлу с внешним клиентом нужен мост sing-box:
                        # без него VLESS применить некуда. Мост поднимает
                        # pool.socks_url_for, поэтому кнопка рабочая —
                        # раньше такие узлы были красными и не нажимались.
                        if not node.is_direct:
                            label = f"{label} (мост)"
                        token = str(index)
                        picks[token] = node.key
                        markup.add(types.InlineKeyboardButton(
                            label,
                            callback_data=f"pool_px:{bid}:{page}:{token}",
                            style="success" if res.usable else "primary",
                        ))
                    self._px_picks = picks
                    markup.add(types.InlineKeyboardButton(
                        "🔄 Проверить прокси", callback_data=f"pool_refresh:{bid}:{page}",
                        style="success",
                    ))

                markup.add(types.InlineKeyboardButton("✏️ Ввести прокси вручную", callback_data=f"bot_proxy_custom:{bid}:{page}", style="primary"))
                markup.add(types.InlineKeyboardButton("🚫 Сбросить прокси (Свой IP)", callback_data=f"bot_proxy_reset:{bid}:{page}", style="danger"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад к боту", callback_data=f"bot_view:{bid}:{page}", style="primary"))

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

            elif data.startswith("pool_px:"):
                _, bid_s, page_s, token = data.split(":", 3)
                bid = int(bid_s)
                page = int(page_s)
                pool = getattr(self.manager, "proxy_pool", None)
                picks = getattr(self, "_px_picks", None) or {}
                node_key = picks.get(token, token)
                node = pool.find_node(node_key) if pool else None
                if node is None:
                    self.bot.answer_callback_query(
                        call.id, "Меню устарело, открой заново", show_alert=True)
                    return
                # socks_url_for поднимает мост для VLESS/VMess/Trojan/SS и
                # отдаёт локальный socks5. Прямой proxy_url() у таких узлов
                # пустой — из-за этого раньше и было «бот умеет только socks5».
                url = pool.socks_url_for(node)
                if not url:
                    self.bot.answer_callback_query(
                        call.id,
                        f"{node.name}: узел не поднялся. Нужен мост sing-box "
                        f"(BR_VPN_BRIDGE=1) — без него VLESS применить некуда.",
                        show_alert=True,
                    )
                    return
                ok, reason = pool.verify_before_connect(url)
                if not ok:
                    self.bot.answer_callback_query(call.id, f"Не работает: {reason}", show_alert=True)
                    return
                if self.manager.set_bot_proxy(bid, url):
                    self.bot.answer_callback_query(call.id, f"Применён: {node.name}", show_alert=True)
                else:
                    self.bot.answer_callback_query(call.id, "Не вышло", show_alert=True)
                call.data = f"bot_view:{bid}:{page}"
                handle_callbacks(call)

            elif data.startswith("pool_dead:"):
                self.bot.answer_callback_query(
                    call.id,
                    "Этот узел требует внешний клиент (VLESS/VMess/Trojan). "
                    "Бот умеет только socks5/http.",
                    show_alert=True,
                )

            elif data.startswith("pool_refresh:"):
                _, bid_s, page_s = data.split(":")
                bid = int(bid_s)
                page = int(page_s)
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.answer_callback_query(call.id, "Пул прокси не запущен", show_alert=True)
                    return
                self.bot.answer_callback_query(call.id, "Проверяю узлы...", show_alert=False)
                threading.Thread(target=pool.refresh, daemon=True).start()
                call.data = f"bot_proxy_menu:{bid}:{page}"
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

            elif data == "ai_menu":
                cfg = self.ai.config
                b_url = cfg.get("base_url", "https://api.openai.com/v1")
                key_masked = ("..." + cfg.get("api_key")[-4:]) if cfg.get("api_key") else "<i>не задан</i>"
                model = cfg.get("model_id", "gpt-4o-mini")

                text = (
                    f"<b>🧠 Меню Нейроконтроллера (ИИ Командир)</b>\n\n"
                    f"Текущие настройки:\n"
                    f"• <b>Base URL:</b> <code>{b_url}</code>\n"
                    f"• <b>API Key:</b> <code>{key_masked}</code>\n"
                    f"• <b>Модель:</b> <code>{model}</code>\n\n"
                    f"Вы можете дать ИИ любую задачу естественным языком, например:\n"
                    f"<i>«Запусти 5 случайных ботов на Воронеж качать лвл»</i>\n"
                    f"<i>«Проверь всех аккаунты на баны и покажи статистику»</i>\n"
                    f"<i>«Останови всех ботов кроме 12 и 13»</i>"
                )

                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(types.InlineKeyboardButton("💬 Дать задачу ИИ (текстом)", callback_data="ai_prompt_task", style="primary"))
                markup.add(types.InlineKeyboardButton("🌐 Изменить Base URL", callback_data="ai_change_base_url", style="primary"))
                markup.add(types.InlineKeyboardButton("🔑 Изменить API Token", callback_data="ai_change_api_key", style="primary"))
                markup.add(types.InlineKeyboardButton("🤖 Изменить ID Модели", callback_data="ai_change_model", style="primary"))
                markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="page:1", style="primary"))

                try:
                    self.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass
                self.bot.answer_callback_query(call.id)

            elif data == "ai_prompt_task":
                self.user_states[chat_id] = {"action": "ai_natural_command"}
                msg = (
                    "<b>💬 Напишите задачу для ИИ:</b>\n\n"
                    "Примеры:\n"
                    "• <i>Запусти ботов на сервер VORONEZH</i>\n"
                    "• <i>Распредели всех по случайным серверам</i>\n"
                    "• <i>Останови всех ботов</i>\n"
                    "• <i>Поставь всем задачу качать лвл</i>\n\n"
                    "Для отмены отправьте /cancel"
                )
                self.bot.send_message(chat_id, msg)
                self.bot.answer_callback_query(call.id)

            elif data == "ai_change_base_url":
                self.user_states[chat_id] = {"action": "ai_set_base_url"}
                self.bot.send_message(chat_id, "🌐 Отправьте новый Base URL (например, <code>https://api.openai.com/v1</code> или локальный Ollama / OpenRouter / vLLM):")
                self.bot.answer_callback_query(call.id)

            elif data == "ai_change_api_key":
                self.user_states[chat_id] = {"action": "ai_set_api_key"}
                self.bot.send_message(chat_id, "🔑 Отправьте ваш API-ключ / токен:")
                self.bot.answer_callback_query(call.id)

            elif data == "ai_change_model":
                self.user_states[chat_id] = {"action": "ai_set_model"}
                self.bot.send_message(chat_id, "🤖 Отправьте ID модели (например, <code>gpt-4o-mini</code>, <code>claude-3-5-sonnet</code>, <code>deepseek-chat</code>, <code>qwen2.5</code>):")
                self.bot.answer_callback_query(call.id)

            elif data == "act_check_bans":
                # Запуск мгновенной проверки банов по всем аккаунтам
                self.bot.answer_callback_query(call.id, "Запуск сканирования банов...", show_alert=False)
                banned_now = []
                with self.manager._lock:
                    for bid, b in self.manager.bots.items():
                        if b.is_banned:
                            banned_now.append(f"• #{bid} <b>{b.nickname}</b> @ {b.server_name}")

                banned_count = len(banned_now)
                report = f"🛡 <b>Результаты сканирования банов:</b>\n\n"
                if banned_count == 0:
                    report += "✅ Забаненных аккаунтов не обнаружено!"
                else:
                    report += f"🔴 Обнаружено забаненных аккаунтов: <b>{banned_count}</b>\n\n" + "\n".join(banned_now[:25])
                    if banned_count > 25:
                        report += f"\n<i>...и еще {banned_count - 25} аккаунтов</i>"

                markup = types.InlineKeyboardMarkup(row_width=1)
                markup.add(types.InlineKeyboardButton("⬅️ Назад в меню", callback_data="page:1", style="primary"))
                try:
                    self.bot.edit_message_text(report, chat_id=chat_id, message_id=message_id, reply_markup=markup)
                except Exception:
                    pass

            elif data in ("vpn_menu", "act_proxies_list"):
                self._render_vpn_menu(chat_id, message_id, call.id)

            elif data.startswith("vpn_page:"):
                page = int(data.split(":", 1)[1] or 1)
                self._render_vpn_menu(chat_id, message_id, call.id, page=page)

            elif data == "vpn_refresh":
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.answer_callback_query(call.id, "Пул прокси не запущен", show_alert=True)
                    return
                self.bot.answer_callback_query(call.id, "Проверяю узлы...")
                threading.Thread(target=pool.refresh, daemon=True).start()
                self._render_vpn_menu(chat_id, message_id, call.id, quiet=True)

            elif data == "vpn_add_sub":
                self.user_states[chat_id] = {"action": "add_vpn_sub"}
                self.bot.send_message(
                    chat_id,
                    "<b>🔗 Новая VPN-подписка или ключ</b>\n\n"
                    "Можно прислать:\n"
                    "• ссылку подписки — <code>https://panel.example/sub/xxxxx</code>\n"
                    "• один ключ — <code>vless://uuid@host:443?...#Имя</code>\n"
                    "• пачку ключей, по одному в строке\n\n"
                    "С именем через <code>|</code>:\n"
                    "<code>Германия|https://panel.example/sub/xxxxx</code>\n\n"
                    "Для отмены: /cancel",
                )
                self.bot.answer_callback_query(call.id)

            elif data == "vpn_add_local":
                self.user_states[chat_id] = {"action": "add_vpn_local"}
                self.bot.send_message(
                    chat_id,
                    "<b>🔌 Локальный прокси или ключ</b>\n\n"
                    "Адрес как есть или с именем через <code>|</code>:\n"
                    "<code>socks5://127.0.0.1:10808</code>\n"
                    "<code>HAPP|socks5://127.0.0.1:10808</code>\n\n"
                    "Ключ <code>vless://</code> тоже принимается — он поднимется "
                    "через мост sing-box, если тот стоит на сервере.",
                )
                self.bot.answer_callback_query(call.id)

            elif data == "vpn_apply_all":
                pool = getattr(self.manager, "proxy_pool", None)
                working = pool.usable_proxies() if pool else []
                if not working:
                    self.bot.answer_callback_query(
                        call.id, "Нет рабочих прокси — сначала проверь", show_alert=True
                    )
                    return
                with self.manager._lock:
                    targets = list(self.manager.bots.values())
                for index, item in enumerate(targets):
                    item.proxy = working[index % len(working)]
                self.manager.save_accounts()
                self.bot.answer_callback_query(
                    call.id,
                    f"Роздано {len(working)} прокси на {len(targets)} ботов",
                    show_alert=True,
                )
                self._render_vpn_menu(chat_id, message_id, call.id, quiet=True)

            elif data == "vpn_noop":
                self.bot.answer_callback_query(call.id)

            elif data.startswith("vpn_pick:"):
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.answer_callback_query(call.id, "Пул прокси не запущен", show_alert=True)
                    return
                token = data.split(":", 1)[1]
                # В кнопке короткий номер: полный ключ узла не влезает в
                # callback_data (лимит 64 байта).
                picks = getattr(self, "_vpn_picks", None) or {}
                key = picks.get(token, token)
                node = pool.find_node(key)
                if node is None:
                    self.bot.answer_callback_query(
                        call.id, "Меню устарело, открой заново", show_alert=True)
                    return
                if pool.is_banned(key):
                    self.bot.answer_callback_query(
                        call.id,
                        f"{node.name}: адрес забанен сервером, раздавать нельзя",
                        show_alert=True,
                    )
                    return
                url = pool.socks_url_for(node)
                if not url:
                    self.bot.answer_callback_query(
                        call.id,
                        f"{node.name}: узел нерабочий или не поднялся мост",
                        show_alert=True,
                    )
                    return
                ok, reason = pool.verify_before_connect(url)
                if not ok:
                    self.bot.answer_callback_query(
                        call.id, f"{node.name}: {reason}", show_alert=True
                    )
                    self._render_vpn_menu(chat_id, message_id, call.id, quiet=True)
                    return
                result = self.manager.apply_proxy_to_bots(url)
                self.bot.answer_callback_query(
                    call.id,
                    f"{node.name}: роздано {len(result.get('applied', []))} ботам",
                    show_alert=True,
                )
                self._render_vpn_menu(chat_id, message_id, call.id, quiet=True)

            elif data == "vpn_rotate":
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.answer_callback_query(call.id, "Пул прокси не запущен", show_alert=True)
                    return
                with self.manager._lock:
                    targets = list(self.manager.bots.values())
                if not targets:
                    self.bot.answer_callback_query(call.id, "Ботов нет", show_alert=True)
                    return
                plan = pool.rotate(len(targets), per_node=3)
                used = 0
                for item, proxy in zip(targets, plan):
                    if proxy:
                        item.proxy = proxy
                        used += 1
                self.manager.save_accounts()
                nodes = len({p for p in plan if p})
                self.bot.answer_callback_query(
                    call.id,
                    f"Ротация: {used} ботов на {nodes} узлов (по 3 на узел)",
                    show_alert=True,
                )
                self._render_vpn_menu(chat_id, message_id, call.id, quiet=True)

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
            if action == "batch_count":
                # Шаг 2: количество. Старую однострочную форму убрали —
                # с телефона помнить порядок полей и точное имя сервера
                # было мучением.
                raw = message.text.strip()
                settings = getattr(self.manager, "settings", None)
                limit = settings.batch_limit if settings else 50
                if not raw.isdigit():
                    self.bot.reply_to(message, "❌ Нужно число. Например: <code>5</code>")
                    return
                count = int(raw)
                if count < 1:
                    self.bot.reply_to(message, "❌ Минимум 1 аккаунт.")
                    return
                if count > limit:
                    self.bot.reply_to(
                        message, f"⚠️ Лимит партии — {limit}. Уменьши количество.")
                    return
                state = self.user_states.get(message.chat.id) or {}
                self._ask_batch_password(message.chat.id, state.get("server", ""), count)

            elif action == "batch_password":
                state = self.user_states.get(message.chat.id) or {}
                password = message.text.strip()
                if len(password) < 5:
                    self.bot.reply_to(
                        message, "❌ Пароль короче 5 символов сервер не примет.")
                    return
                self._ask_batch_task(
                    message.chat.id, state.get("server", ""),
                    state.get("count", 1), password)

            elif action == "set_base_password":
                del self.user_states[message.chat.id]
                settings = getattr(self.manager, "settings", None)
                if settings is None:
                    self.bot.reply_to(message, "❌ Настройки недоступны.")
                    return
                password = message.text.strip()
                if len(password) < 5:
                    self.bot.reply_to(message, "❌ Пароль короче 5 символов.")
                    return
                settings.base_password = password
                self.bot.reply_to(
                    message,
                    f"✅ Базовый пароль сохранён: <code>{password}</code>\n"
                    f"Его получат все новые аккаунты.")
                self._render_settings(message.chat.id)

            elif action == "set_batch_limit":
                del self.user_states[message.chat.id]
                settings = getattr(self.manager, "settings", None)
                if settings is None:
                    self.bot.reply_to(message, "❌ Настройки недоступны.")
                    return
                raw = message.text.strip()
                if not raw.isdigit():
                    self.bot.reply_to(message, "❌ Нужно число от 1 до 500.")
                    return
                settings.batch_limit = int(raw)
                self.bot.reply_to(
                    message, f"✅ Лимит партии: <b>{settings.batch_limit}</b>")
                self._render_settings(message.chat.id)

            elif action in ("start_selected_bots", "stop_selected_bots",
                            "proxy_selected_bots", "goal_selected_bots",
                            "del_selected_bots"):
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                selected_list = self._parse_ids(message.text)

                if not selected_list:
                    self.bot.reply_to(message, "❌ Не распознано ни одного ID бота. Пример: <code>1-5, 8, 10</code>")
                    return

                if action == "stop_selected_bots":
                    stopped = self.manager.stop_selected(selected_list)
                    self.bot.reply_to(
                        message,
                        f"⏹ Остановлены ({len(stopped)} шт.): "
                        + ", ".join(f"#{i}" for i in stopped[:15])
                        if stopped else "⚠️ Таких ботов нет или они уже стоят.",
                    )
                    return

                if action == "proxy_selected_bots":
                    self._ask_bulk_proxy(message.chat.id, selected_list, page)
                    return

                if action == "goal_selected_bots":
                    self._goal_ids = selected_list
                    self.user_states[message.chat.id] = {
                        "action": "goal_level", "page": page}
                    self.bot.reply_to(
                        message,
                        f"<b>🎯 Цель для {len(selected_list)} бот(ов)</b>\n\n"
                        f"Пришли уровень, до которого качаться "
                        f"(например <code>10</code>).\n"
                        f"<code>0</code> — снять цель.\n\n"
                        f"Для отмены: /cancel",
                    )
                    return

                if action == "del_selected_bots":
                    known = [i for i in selected_list if i in self.manager.bots]
                    if not known:
                        self.bot.reply_to(message, "⚠️ Таких ботов нет.")
                        return
                    self._del_some_ids = known
                    markup = types.InlineKeyboardMarkup(row_width=2)
                    markup.row(
                        types.InlineKeyboardButton(
                            f"🗑 Удалить {len(known)}",
                            callback_data="del_some_confirm", style="danger"),
                        types.InlineKeyboardButton(
                            "↩️ Отмена", callback_data=f"page:{page}", style="primary"),
                    )
                    names = ", ".join(
                        f"#{i} {self.manager.bots[i].nickname}" for i in known[:12])
                    tail = f" и ещё {len(known) - 12}" if len(known) > 12 else ""
                    self.bot.reply_to(
                        message,
                        f"<b>🗑 Удалить {len(known)} аккаунт(ов)?</b>\n\n"
                        f"{names}{tail}\n\n"
                        f"<i>Аккаунты будут остановлены и убраны из реестра. "
                        f"Отменить это нельзя.</i>",
                        reply_markup=markup,
                    )
                    return

                ids_to_start = set(selected_list)
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

            elif action == "goal_level":
                page = state.get("page", 1)
                del self.user_states[message.chat.id]
                ids = getattr(self, "_goal_ids", None) or []
                self._goal_ids = []
                raw = (message.text or "").strip().lower()
                if not raw.isdigit():
                    self.bot.reply_to(message, "❌ Нужно число. Например <code>10</code>.")
                    return
                level = int(raw)
                changed = self.manager.set_target_level(ids, level)
                if level <= 0:
                    self.bot.reply_to(
                        message,
                        f"🎯 Цель снята у <b>{len(changed)}</b> аккаунт(ов).")
                else:
                    self.bot.reply_to(
                        message,
                        f"🎯 Цель <b>{level} lvl</b> поставлена "
                        f"<b>{len(changed)}</b> аккаунт(ам).")

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

            elif action == "ai_set_base_url":
                del self.user_states[message.chat.id]
                new_url = message.text.strip()
                self.ai.update_settings(base_url=new_url)
                self.bot.reply_to(message, f"✅ Base URL сохранен:\n<code>{new_url}</code>")

            elif action == "ai_set_api_key":
                del self.user_states[message.chat.id]
                new_key = message.text.strip()
                self.ai.update_settings(api_key=new_key)
                self.bot.reply_to(message, "✅ API-ключ сохранен!")

            elif action == "ai_set_model":
                del self.user_states[message.chat.id]
                new_model = message.text.strip()
                self.ai.update_settings(model_id=new_model)
                self.bot.reply_to(message, f"✅ Модель сохранена:\n<code>{new_model}</code>")

            elif action == "ai_natural_command":
                del self.user_states[message.chat.id]
                prompt = message.text.strip()
                status_msg = self.bot.reply_to(message, "🧠 <i>ИИ анализирует задачу и управляет ботами...</i>", parse_mode="HTML")
                res = self.ai.execute_natural_command(prompt)
                try:
                    self.bot.edit_message_text(res, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="HTML")
                except Exception:
                    self.bot.send_message(message.chat.id, res, parse_mode="HTML")

            elif action == "add_vpn_sub":
                del self.user_states[message.chat.id]
                raw_val = message.text.strip()
                if "|" in raw_val:
                    sub_name, sub_url = raw_val.split("|", 1)
                    sub_name, sub_url = sub_name.strip(), sub_url.strip()
                else:
                    sub_name, sub_url = "", raw_val
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.reply_to(message, "❌ Пул прокси не запущен.")
                    return
                status = self.bot.reply_to(message, "⏳ Принимаю...")
                try:
                    nodes = pool.add_any(sub_url, name=sub_name)
                except Exception as exc:
                    self.bot.edit_message_text(
                        f"❌ Не вышло: <code>{exc}</code>",
                        chat_id=message.chat.id,
                        message_id=status.message_id,
                    )
                    return
                self.bot.edit_message_text(
                    f"✅ Принято, узлов: <b>{len(nodes)}</b>. Проверяю...",
                    chat_id=message.chat.id,
                    message_id=status.message_id,
                )
                threading.Thread(target=pool.refresh, kwargs={"fetch": False}, daemon=True).start()
                time.sleep(1.0)
                self._render_vpn_menu(message.chat.id, None, None, quiet=True)

            elif action == "add_vpn_local":
                del self.user_states[message.chat.id]
                raw_val = message.text.strip()
                # Имя необязательно: ключ vless:// или socks5:// принимается
                # как есть, без префикса «Имя|».
                if "|" in raw_val and not raw_val.lower().startswith(
                    ("vless://", "vmess://", "trojan://", "ss://", "ssr://",
                     "socks5://", "socks5h://", "socks4://")
                ):
                    local_name, local_proxy = raw_val.split("|", 1)
                else:
                    local_name, local_proxy = "", raw_val
                pool = getattr(self.manager, "proxy_pool", None)
                if pool is None:
                    self.bot.reply_to(message, "❌ Пул прокси не запущен.")
                    return
                try:
                    node = pool.add_manual(local_proxy.strip(), name=local_name.strip())
                except Exception as exc:
                    self.bot.reply_to(message, f"❌ Не вышло: <code>{exc}</code>")
                    return
                # Проверяем так, как бот реально пойдёт: для vless это мост
                # sing-box и его SOCKS5-вход.
                url = pool.socks_url_for(node)
                if not url:
                    self.bot.reply_to(
                        message,
                        f"⚠️ <b>{node.name}</b>: узел принят, но применить пока "
                        f"нельзя — нужен мост sing-box на сервере "
                        f"(<code>apt install sing-box</code>).",
                    )
                else:
                    ok, reason = pool.verify_before_connect(url)
                    mark = "✅" if ok else "⚠️"
                    self.bot.reply_to(
                        message, f"{mark} <b>{node.name}</b>: {reason or 'ок'}"
                    )
                self._render_vpn_menu(message.chat.id, None, None, quiet=True)

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
        # Автообновление открытых окон: статус меняется сам, перезаходить в
        # меню не нужно.
        self._start_live_refresh()
        print("[+] Telegram бот управления успешно запущен!")

    def stop(self):
        self.running = False
        try:
            self.bot.stop_polling()
        except Exception:
            pass
