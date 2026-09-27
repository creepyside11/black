#!/usr/bin/env python3
# Engine: SA-MP / Black Russia RakNet | Python 3 | Termux
# Build: pip install -r requirements.txt (if any)
# Run: python3 bot.py [сервер/номер] [никнейм] [пароль]

import sys
import os
import time
import random
import threading
import json
import resource
from collections import deque

# Оптимизация системы для поддержки до 300+ ботов одновременно в Termux / Linux
try:
    threading.stack_size(256 * 1024)  # 256 KB стек потока вместо 8 MB
except Exception:
    pass

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 8192), max(hard, 8192)))
except Exception:
    pass

import br_servers
from core.network import Network
from core import security
from core import logger
from core.protocol import RakNetProtocol
from samp.api import BotAPI
from samp.session import SampSession
from samp.sync import PlayerSyncScheduler
from logic.registration import RegistrationConfig
from logic.json_ui import JsonUIConfig
from logic.events import emitter
from logic.bot_tasks import LevelGrinderTask, QuestMasterTask, MinerTask, FactoryTask, BusDriverTask
from tg_controller import TelegramBotController

MAX_NICKNAME_LEN = 20
MAX_LOG_HISTORY = 100
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")
PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")


class BotInstance:
    def __init__(
        self,
        bot_id: int,
        host: str,
        port: int,
        nickname: str,
        password: str,
        server_name: str = "",
        auto_regen_nick: bool = False,
        manager: any = None,
        is_banned: bool = False,
        default_task: str = "lvl",
        proxy: str = "",
    ):
        self.bot_id = bot_id
        self.host = host
        self.port = int(port)
        self.nickname = nickname
        self.password = password
        self.server_name = server_name.upper()
        self.auto_regen_nick = auto_regen_nick
        self.manager = manager
        self.stopping = False
        self.is_banned = is_banned
        self.default_task_name = default_task
        self.proxy = proxy.strip() if proxy else ""

        self.level_str = "-"
        self.money = 0
        self.target_lvl = None  # Целевой уровень (например 4)
        self.target_achieved = False
        self.last_stats_check = 0.0
        self.stats_pending = False
        self.stats_dialog_id = None

        self.logs = deque(maxlen=MAX_LOG_HISTORY)
        self.status = "ЗАБАНЕН" if self.is_banned else "Остановлен"
        self.connected_time = None
        self.thread = None
        self.active_task = None

        self.reconnect_thread = None
        self.reconnect_cancel_event = threading.Event()
        self.scheduled_reconnect_delay = None
        self.stats_timer_thread = None

        self.session = None
        self.api = None
        self.network = None
        self.protocol = None
        self.sync_scheduler = None

    def get_numeric_level(self) -> int:
        if self.level_str and self.level_str != "-":
            m = re.search(r'(\d+)', self.level_str)
            if m:
                return int(m.group(1))
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            return int(getattr(self.session.players.bot, "score", 0))
        return 0

    def check_target_level_achieved(self):
        if self.target_lvl and not self.target_achieved:
            cur_lvl = self.get_numeric_level()
            if cur_lvl >= self.target_lvl:
                self.target_achieved = True
                self.add_log(f"🎯 ЦЕЛЬ ДОСТИГНУТА! Получен уровень {cur_lvl} (цель: {self.target_lvl} lvl). Бот останавливается.")
                if self.manager and hasattr(self.manager, "notify_users"):
                    msg = (
                        f"🎯 <b>Цель аккаунта достигнута!</b>\n\n"
                        f"Бот: <b>#{self.bot_id} [{self.nickname}]</b>\n"
                        f"Сервер: <b>{self.server_name}</b>\n"
                        f"Текущий уровень: <b>{self.level_str}</b>\n"
                        f"Целевой уровень: <b>{self.target_lvl} lvl</b>\n\n"
                        f"<i>Аккаунт успешно остановлен.</i>"
                    )
                    self.manager.notify_users(msg)
                self.stop()
                if self.manager:
                    self.manager.save_accounts()

    def get_level(self) -> str:
        # 1. Если спарсили детальный уровень с EXP через диалог /mm
        if self.level_str and self.level_str != "-":
            return self.level_str
        # 2. Иначе берем игровой score из сетевых пакетов SA-MP (scoreboard/score)
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            s = int(getattr(self.session.players.bot, "score", 0))
            if s > 0:
                self.level_str = f"{s} lvl"
                return self.level_str
        return self.level_str or "-"

    def get_balance_str(self) -> str:
        # Проверяем баланс из сессии или кэша
        if self.session and hasattr(self.session, "players") and hasattr(self.session.players, "bot"):
            m = int(getattr(self.session.players.bot, "money", 0))
            if m > 0:
                self.money = m
        if self.money > 0:
            return f"{self.money:,} руб.".replace(",", " ")
        return "0 руб."

    def add_log(self, text: str):
        t_str = time.strftime("[%H:%M:%S]")
        formatted = f"{t_str} [{self.nickname}] {text}"
        self.logs.append(formatted)
        return formatted

    def setup_session(self):
        self.stopping = False
        self.network = Network(self.host, self.port)
        self.protocol = RakNetProtocol(self.network, security)
        self.sync_scheduler = PlayerSyncScheduler()

        reg_config = None
        if self.password:
            reg_config = RegistrationConfig(
                enabled=True,
                auto_login=True,
                password=self.password,
                gender=0,
                skin=78,
                step_delay=0.6,
                ack_timeout=15.0,
            )

        json_ui = JsonUIConfig(
            console_output=False,
            auto_close_cinematic=True,
        )

        server_names_map = {}
        if self.manager and hasattr(self.manager, "server_names_cache"):
            server_names_map = self.manager.server_names_cache

        self.session = SampSession(
            self.protocol,
            self.host,
            self.port,
            self.nickname,
            sync_scheduler=self.sync_scheduler,
            fps=60.0,
            pings_ips_continuous=False,
            server_password=None,
            http_pre_ping=True,
            http_pre_ping_port=80,
            http_pre_ping_timeout=5.0,
            http_pre_ping_host_suffix="blackrussia.online",
            http_server_names=server_names_map,
            registration=reg_config,
            json_ui=json_ui,
        )

        self.api = BotAPI(self.session)
        self.network.set_proxy_manager(self.session.proxy)

        if self.proxy:
            try:
                self.session.proxy.connect(self.proxy, timeout=5.0)
                self.add_log(f"Настроен SOCKS5 прокси: {self.proxy}")
            except Exception as e:
                self.add_log(f"Ошибка настройки прокси {self.proxy}: {e}")

    def start(self):
        if self.is_banned:
            self.add_log("Попытка запуска забаненного аккаунта отклонена.")
            return False

        self.cancel_reconnect()
        self.stopping = False
        self.status = "Подключение..."
        self.setup_session()

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

        if not self.stats_timer_thread or not self.stats_timer_thread.is_alive():
            self.stats_timer_thread = threading.Thread(target=self._stats_checker_loop, daemon=True)
            self.stats_timer_thread.start()

        return True

    def _stats_checker_loop(self):
        """Регулярная проверка /mm -> Статистика раз в 30 минут"""
        while not self.stopping and not self.is_banned:
            time.sleep(30)
            if self.stopping or self.is_banned:
                break
            if self.session and self.session.connected and "В игре" in self.status:
                now = time.time()
                # Если прошло 30 минут с прошлой проверки
                if now - self.last_stats_check >= 1800:
                    self.stats_pending = True
                    self.send_chat("/mm")
                    self.last_stats_check = now

    def _run(self):
        try:
            connected = self.api.connect()
            if not connected and not self.stopping and not self.is_banned:
                # Если сервер не ответил на хэндшейк (сервер полон или пакет потерялся),
                # повторяем быстро через 5-10 сек, а не ждём 5 минут!
                retry_delay = round(random.uniform(5.0, 10.0), 1)
                self.add_log(f"Сервер не ответил на подключение. Повтор через {retry_delay}с...")
                self.schedule_reconnect(delay=retry_delay)
        except Exception as e:
            if not self.stopping and not self.is_banned:
                self.status = f"Ошибка: {e}"
                self.add_log(f"Критическая ошибка сессии: {e}")
                self.schedule_reconnect(delay=10.0)

    def schedule_reconnect(self, delay: float = 10.0):
        if self.stopping or self.is_banned:
            return
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return

        self.reconnect_cancel_event.clear()
        target_time = time.time() + delay
        self.add_log(f"Связь разорвана/ожидание. Авто-вход через {int(delay)} сек...")

        def _reconnect_waiter():
            while time.time() < target_time:
                if self.reconnect_cancel_event.is_set() or self.stopping or self.is_banned:
                    return
                rem = max(0, int(target_time - time.time()))
                m, s = divmod(rem, 60)
                self.status = f"Ожидание ({m:02d}:{s:02d})"
                time.sleep(1.0)

            if self.reconnect_cancel_event.is_set() or self.stopping or self.is_banned:
                return

            self.add_log(f"Ожидание завершено ({int(delay)} сек). Автоматический перезапуск сессии...")
            self.start()

        self.reconnect_thread = threading.Thread(target=_reconnect_waiter, daemon=True)
        self.reconnect_thread.start()

    def cancel_reconnect(self):
        self.reconnect_cancel_event.set()

    def send_chat(self, msg: str):
        if self.api:
            self.api.sendchat(msg)
            self.add_log(f">> {msg}")

    def set_task(self, task_type: str):
        if self.active_task:
            self.active_task.stop()
            self.active_task = None

        self.default_task_name = task_type
        if self.manager:
            self.manager.save_accounts()

        if task_type == "lvl":
            self.active_task = LevelGrinderTask(self)
            self.active_task.start()
            return "Качка LVL запущена"
        elif task_type == "quest":
            self.active_task = QuestMasterTask(self)
            self.active_task.start()
            return "Квесты Дяди Славы запущены"
        elif task_type in ("mine", "miner", "mining", "шахта"):
            self.active_task = MinerTask(self)
            self.active_task.start()
            return "Бот шахтера запущен"
        elif task_type in ("factory", "zavod", "завод"):
            self.active_task = FactoryTask(self)
            self.active_task.start()
            return "Бот завода запущен"
        elif task_type in ("bus", "автобус", "driver"):
            self.active_task = BusDriverTask(self)
            self.active_task.start()
            return "Бот водителя автобуса запущен"
        elif task_type in ("stop", "none", "off"):
            return "Авто-задача остановлена"
        else:
            return "Неизвестный тип задачи. Доступно: 'lvl', 'quest', 'mine', 'factory', 'bus', 'stop'"

    def stop(self, banned: bool = False):
        self.stopping = True
        self.cancel_reconnect()
        if self.active_task:
            try:
                self.active_task.stop()
            except Exception:
                pass
            self.active_task = None
        self.status = "ЗАБАНЕН" if (banned or self.is_banned) else "Отключен"
        if self.api:
            try:
                self.api.disconnect()
            except Exception:
                pass


class BotManager:
    def __init__(self):
        self.bots: dict[int, BotInstance] = {}
        self._next_id = 1
        self.active_bot_id: int | None = None
        self._lock = threading.Lock()
        self.stream_active_logs = True

        # Кэш соответствия IP серверов и их названий (для правильного HTTP pre-ping заголовка Host)
        self.server_names_cache = {}
        try:
            all_s = br_servers.get_servers()
            self.server_names_cache = {s["ip"]: s.get("firstname", "") for s in all_s}
        except Exception:
            self.server_names_cache = {}

        logger.set_log_interceptor(self._handle_global_log)
        self._setup_event_hooks()

        # Загружаем сохраненные аккаунты из accounts.json
        self.load_accounts()

    def _handle_global_log(self, raw_message: str) -> bool:
        assigned = False
        with self._lock:
            for b in self.bots.values():
                if b.nickname and b.nickname in raw_message:
                    b.logs.append(raw_message)
                    assigned = True
                    break

            if not assigned and self.active_bot_id is not None:
                cur = self.bots.get(self.active_bot_id)
                if cur:
                    cur.logs.append(raw_message)

            if not self.stream_active_logs:
                return False

            if self.active_bot_id is not None:
                cur = self.bots.get(self.active_bot_id)
                if cur and cur.nickname and cur.nickname in raw_message:
                    return True
                # Если лог не привязан к активному боту, не выводим в общую консоль!
                return False
            # Если активный бот не выбран, подавляем шум подключения/киков
            return False

    def _find_bot_by_session_or_api(self, bot_obj):
        with self._lock:
            for b in self.bots.values():
                if b.api == bot_obj or b.session == bot_obj:
                    return b
        return None

    def mark_bot_banned(self, bot: BotInstance, reason: str = ""):
        bot.is_banned = True
        bot.cancel_reconnect()
        bot.status = "ЗАБАНЕН"
        bot.add_log(f"[!] АККАУНТ ЗАБАНЕН СЕРВЕРОМ! Причина: {reason}")
        print(f"\n[!] ВНИМАНИЕ: Бот #{bot.bot_id} [{bot.nickname}] получил БАН! ({reason})")
        bot.stop(banned=True)
        self.save_accounts()

    def _setup_event_hooks(self):
        @emitter.on("onconnect")
        def _on_connected(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.status = "В игре (авторизован)"
                b.connected_time = time.time()
                b.add_log("Подключение установлено, мир загружен")
                if b.default_task_name and not b.active_task:
                    b.set_task(b.default_task_name)

        @emitter.on("onSpawn")
        def _on_spawn(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.status = "В игре (заспавнен)"
                b.add_log("Персонаж заспавнен на карте")
                # При спавне запрашиваем scores/pings и /mm
                def _initial_stats_query():
                    time.sleep(1.5)
                    if b.api and b.session and b.session.connected:
                        try:
                            b.api.sendscoresandpings()
                        except Exception:
                            pass
                    time.sleep(2.0)
                    if b.api and "В игре" in b.status:
                        b.stats_pending = True
                        b.send_chat("/mm")
                threading.Thread(target=_initial_stats_query, daemon=True).start()

        @emitter.on("onShowChat")
        def _on_chat(bot_obj, message, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ЧАТ] {message}")
                if b.active_task and hasattr(b.active_task, "on_chat_message"):
                    b.active_task.on_chat_message(str(message))
                msg_lower = str(message).lower()
                clean_msg = re.sub(r'\{[0-9a-fA-F]{6}\}', '', str(message))
                clean_msg_lower = clean_msg.lower()

                # 1. Проверяем бан аккаунта / блокировку администратором
                # Примеры: "Администратор X забанил Y", "Вы были заблокированы", "аккаунт забанен"
                admin_ban_patterns = [
                    r'администратор\s+.*\s+(?:забанил|заблокировал|выдал\s+бан)\s+.*' + re.escape(b.nickname.lower()),
                    r'(?:вы\s+были\s+)?заблокированы?\s+администратор',
                    r'аккаунт\s+.*заблокирован',
                ]
                is_admin_ban = any(re.search(pat, clean_msg_lower) for pat in admin_ban_patterns)
                if not is_admin_ban and b.nickname.lower() in clean_msg_lower:
                    ban_words = ["заблокирован", "забанен", "получил бан", "выдан бан", "banned"]
                    if any(k in clean_msg_lower for k in ban_words) and "чат" not in clean_msg_lower and "мут" not in clean_msg_lower:
                        is_admin_ban = True

                if is_admin_ban:
                    self.mark_bot_banned(b, reason=f"Чат: {clean_msg}")
                    return

                # 2. Бан на 20 минут за частые входы -> реконнект через 25 минут (1500 сек)
                if "забанен" in clean_msg_lower and ("20 минут" in clean_msg_lower or "20 мин" in clean_msg_lower or "частые входы" in clean_msg_lower or "частых входов" in clean_msg_lower):
                    b.add_log("[РЕКОННЕКТ] Бан за частые входы (20 мин). Повторный вход запланирован через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0

                # 3. Много заходов с вашего IP -> реконнект через 5 минут (300 сек)
                if "много заходов" in clean_msg_lower or ("ip" in clean_msg_lower and ("слишком много" in clean_msg_lower or "подозрител" in clean_msg_lower or "повторите попытку позже" in clean_msg_lower)):
                    b.add_log("[РЕКОННЕКТ] Сервер сообщил: много заходов с IP. Ожидание 5 минут...")
                    b.scheduled_reconnect_delay = 5 * 60.0

                # 4. Рестарт сервера через 1 минуту -> выход через 30 сек, реконнект через 5 минут
                if "рестарт" in clean_msg_lower and ("через 1 мин" in clean_msg_lower or "1 минуту" in clean_msg_lower or "60 сек" in clean_msg_lower):
                    b.add_log("[РЕСТАРТ] Обнаружено предупреждение о рестарте через 1 минуту. Выход через 30 секунд...")
                    def _safe_restart_exit():
                        time.sleep(30.0)
                        if b and not b.is_banned:
                            b.add_log("[РЕСТАРТ] Выход с сервера перед рестартом. Вход запланирован через 5 минут.")
                            b.scheduled_reconnect_delay = 5 * 60.0
                            b.stop()
                    threading.Thread(target=_safe_restart_exit, daemon=True).start()

        @emitter.on("onShowDialog")
        def _on_dialog(bot_obj, d_id, style, title, info, b1, b2, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ДИАЛОГ #{d_id}] {title} | Кнопки: '{b1}' / '{b2}'")
                if b.active_task and hasattr(b.active_task, "on_dialog"):
                    b.active_task.on_dialog(d_id, style, title, info, b1, b2)

                dialog_text = f"{title} {info}".lower()
                clean_info = re.sub(r'\{[0-9a-fA-F]{6}\}', '', info)

                # 1. Автоматический опрос /mm -> Статистика
                # Меню /mm: выбор "Статистика персонажа" (обычно 0 пункт)
                if ("главное меню" in dialog_text or "меню персонажа" in dialog_text or "mm" in dialog_text) and b.stats_pending:
                    # Находим пункт "Статистика" в списке
                    lines = [ln.strip() for ln in clean_info.split("\n") if ln.strip()]
                    stat_idx = 0
                    for idx, ln in enumerate(lines):
                        if "статистик" in ln.lower():
                            stat_idx = idx
                            break
                    def _pick_stat():
                        time.sleep(0.4)
                        from samp.packets import PacketBuilder
                        if b.session and b.session.connected:
                            b.session._sender.send(
                                PacketBuilder.rpc_build("dialog_response", int(d_id), 1, stat_idx, ""),
                                rpc_name="dialog_response",
                            )
                    threading.Thread(target=_pick_stat, daemon=True).start()

                # Окно со статистикой персонажа (уровень, exp)
                if "статистика" in dialog_text or "уровень" in dialog_text or "exp" in dialog_text:
                    # Ищем уровень и exp: например "Уровень: 2", "Игровой уровень: 2", "Очки опыта: 2/8", "EXP: 2/8", "Опыт: 2 из 8"
                    # Также форматы: "2 lvl", "2/8 exp"
                    lvl_match = re.search(r'(?:уровень|level|lvl)[:\s]+(\d+)', clean_info, re.IGNORECASE)
                    exp_match = re.search(r'(?:exp|опыт|очки опыта|очков)[:\s]+(\d+)\s*[\/из]\s*(\d+)', clean_info, re.IGNORECASE)
                    
                    found_lvl = lvl_match.group(1) if lvl_match else None
                    found_exp = f"{exp_match.group(1)}/{exp_match.group(2)}" if exp_match else None

                    if not found_exp:
                        # Иногда формат просто 2/8
                        exp_fallback = re.search(r'(\d+)\s*\/\s*(\d+)', clean_info)
                        if exp_fallback:
                            found_exp = f"{exp_fallback.group(1)}/{exp_fallback.group(2)}"

                    # Ищем деньги / баланс: например "Баланс: 15,000 руб", "Деньги: 1000", "Наличные: 500 руб"
                    # В диалогах Black Russia часто пишется: "Наличные деньги: 15000 руб.", "Банковский счет: 20000 руб.", "Баланс: 500"
                    money_matches = re.findall(r'(?:баланс|деньги|наличные|рублей|на руках|основной счет|банк(?:\s+счет)?)[^\d\n\r]{0,15}?([\d\s,]+)', clean_info, re.IGNORECASE)
                    if money_matches:
                        for mm in money_matches:
                            raw_m = re.sub(r'[^\d]', '', mm)
                            if raw_m:
                                val = int(raw_m)
                                if val > 0:
                                    b.money = val
                                    break
                    else:
                        money_match = re.search(r'([\d\s,]{2,})\s*(?:руб|rub|\$)', clean_info, re.IGNORECASE)
                        if money_match:
                            raw_m = re.sub(r'[^\d]', '', money_match.group(1))
                            if raw_m and int(raw_m) > 0:
                                b.money = int(raw_m)

                    if found_lvl:
                        if found_exp:
                            b.level_str = f"{found_lvl} ({found_exp})"
                        else:
                            b.level_str = f"{found_lvl} lvl"
                        b.last_stats_check = time.time()
                        b.stats_pending = False
                        b.add_log(f"[СТАТИСТИКА] Обновлен уровень: {b.level_str}, баланс: {b.get_balance_str()}")
                        self.save_accounts()
                        b.check_target_level_achieved()

                    # Закрываем диалог статистики, чтобы не висел
                    def _close_stats():
                        time.sleep(0.3)
                        from samp.packets import PacketBuilder
                        if b.session and b.session.connected:
                            b.session._sender.send(
                                PacketBuilder.rpc_build("dialog_response", int(d_id), 0, 0, ""),
                                rpc_name="dialog_response",
                            )
                    threading.Thread(target=_close_stats, daemon=True).start()

                # Проверяем диалоговые окна на бан / лимиты входа / рестарт
                ban_triggers = [
                    "блокировк",
                    "заблокирован",
                    "забанен",
                    "бан навсегда",
                    "аккаунт заблокирован",
                    "banned",
                    "причина блокировки",
                    "до разблокировки",
                    "вы получили бан",
                    "ваш аккаунт был заблокирован",
                ]
                # Исключаем временный бан за частые входы из перманентного бана
                is_freq_ban = "20 минут" in dialog_text or "20 мин" in dialog_text or "частые входы" in dialog_text or "частых входов" in dialog_text
                if is_freq_ban:
                    b.add_log(f"[РЕКОННЕКТ] Бан за частые входы в диалоге #{d_id}. Вход через 25 минут.")
                    b.scheduled_reconnect_delay = 25 * 60.0
                elif any(trigger in dialog_text for trigger in ban_triggers):
                    self.mark_bot_banned(b, reason=f"Диалог #{d_id}: {title} | {clean_info[:80]}")

                # Много заходов с одного IP в диалоге
                if "много заходов" in dialog_text or ("ip" in dialog_text and ("слишком много" in dialog_text or "подозрител" in dialog_text or "повторите попытку позже" in dialog_text)):
                    b.add_log(f"[РЕКОННЕКТ] Диалог #{d_id}: много заходов с IP. Ожидание 5 минут...")
                    b.scheduled_reconnect_delay = 5 * 60.0

        @emitter.on("OnShowTextDraw")
        def _on_textdraw(bot_obj, textdraw_id, info, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_textdraw"):
                b.active_task.on_textdraw(textdraw_id, info)

        @emitter.on("OnTextDrawSetString")
        def _on_td_set_string(bot_obj, textdraw_id, text, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_textdraw_set_string"):
                b.active_task.on_textdraw_set_string(textdraw_id, text)

        @emitter.on("disconnect")
        def _on_disconnect(bot_obj, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log("Соединение с сервером разорвано")
                if b.is_banned:
                    b.status = "ЗАБАНЕН"
                    return
                if b.stopping:
                    b.status = "Отключен"
                    return

                # Проверяем, был ли задан кастомный таймер ожидания (рестарт, лимит IP, бан за частые входы)
                if b.scheduled_reconnect_delay:
                    reconnect_delay = b.scheduled_reconnect_delay
                    b.scheduled_reconnect_delay = None
                else:
                    # Быстрый повторный вход (5-10 сек), если это обычный реконнект
                    reconnect_delay = round(random.uniform(5.0, 10.0), 1)

                b.schedule_reconnect(delay=reconnect_delay)

        @emitter.on("onKick")
        def _on_kick(bot_obj, reason_code, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[КИК] Отключен сервером. Код причины: {reason_code}")
                if reason_code == 999:
                    self.mark_bot_banned(b, reason="Пакет CONNECTION_BANNED (0x19)")
                    return
                if reason_code == 2 and getattr(b, "auto_regen_nick", False):
                    self.replace_bot_nickname(b)
                    return
                b.status = f"Кикнут (код {reason_code})"

        @emitter.on("onRegistrationState")
        def _on_reg_state(bot_obj, state, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[АВТОРИЗАЦИЯ] Статус: {state}")
                if state == "logging_in" and getattr(b, "auto_regen_nick", False):
                    self.replace_bot_nickname(b)

        @emitter.on("onCallNotification")
        def _on_call(bot_obj, header, text, button, notif_type, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                b.add_log(f"[ВХОДЯЩИЙ ЗВОНОК] {header}: {text}")
                if b.active_task and hasattr(b.active_task, "on_call_notification"):
                    b.active_task.on_call_notification(header, text, button, notif_type)

        @emitter.on("onReceiveJSON")
        def _on_receive_json(bot_obj, interface_id, json_data, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_receive_json"):
                b.active_task.on_receive_json(interface_id, json_data)

        @emitter.on("onQuestTask")
        def _on_quest_task(text, *args, **kwargs):
            with self._lock:
                for b in self.bots.values():
                    if b.active_task and isinstance(b.active_task, QuestMasterTask):
                        b.active_task.on_quest_task(text)

        @emitter.on("onNpcDialog")
        def _on_npc_dialog(name, text, model, buttons, *args, **kwargs):
            with self._lock:
                for b in self.bots.values():
                    if b.active_task and hasattr(b.active_task, "on_npc_dialog"):
                        b.active_task.on_npc_dialog(name, text, model, buttons)

        @emitter.on("OnSetCheckpoint")
        def _on_checkpoint(bot_obj, x, y, z, radius, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b and b.active_task and hasattr(b.active_task, "on_checkpoint"):
                b.active_task.on_checkpoint(x, y, z, radius)

        @emitter.on("OnSetMoney")
        def _on_set_money(bot_obj, amount, *args, **kwargs):
            b = self._find_bot_by_session_or_api(bot_obj)
            if b:
                try:
                    b.money = int(amount)
                    b.add_log(f"[БАЛАНС] Обновлен баланс: {b.get_balance_str()}")
                    self.save_accounts()
                except Exception:
                    pass

        @emitter.on("onRewardList")
        def _on_rewards(rewards, *args, **kwargs):
            with self._lock:
                for b in self.bots.values():
                    if b.active_task and isinstance(b.active_task, QuestMasterTask):
                        b.active_task.on_rewards(rewards)

    def load_accounts(self):
        if not os.path.exists(ACCOUNTS_FILE):
            return

        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return

            loaded_count = 0
            for item in data:
                nick = item.get("nickname", "").strip()
                pwd = item.get("password", "").strip()
                srv_str = item.get("server", "").strip()
                task = item.get("task", "lvl")
                is_banned = bool(item.get("is_banned", False))
                proxy = item.get("proxy", "")
                saved_lvl = item.get("lvl", "-")
                saved_money = int(item.get("money", 0))
                saved_target_lvl = item.get("target_lvl")
                if saved_target_lvl is not None:
                    try:
                        saved_target_lvl = int(saved_target_lvl)
                    except Exception:
                        saved_target_lvl = None

                if not nick or not pwd or not srv_str:
                    continue

                try:
                    srv = br_servers.find_server(srv_str)
                except Exception:
                    srv = None

                if not srv:
                    continue

                clean_name = srv.get("firstname") or srv.get("name", "").replace("BLACK RUSSIA | ", "").strip()
                clean_name = clean_name.upper()
                ip = srv["ip"]
                port = int(srv["port"])

                with self._lock:
                    bot_id = self._next_id
                    self._next_id += 1
                    b = BotInstance(
                        bot_id=bot_id,
                        host=ip,
                        port=port,
                        nickname=nick,
                        password=pwd,
                        server_name=clean_name,
                        auto_regen_nick=False,
                        manager=self,
                        is_banned=is_banned,
                        default_task=task,
                        proxy=proxy,
                    )
                    b.level_str = saved_lvl
                    b.money = saved_money
                    b.target_lvl = saved_target_lvl
                    self.bots[bot_id] = b
                    if self.active_bot_id is None:
                        self.active_bot_id = bot_id
                loaded_count += 1

            if loaded_count > 0:
                print(f"[+] Загружено {loaded_count} сохраненных аккаунтов из {os.path.basename(ACCOUNTS_FILE)}")
        except Exception as e:
            print(f"[!] Ошибка загрузки {ACCOUNTS_FILE}: {e}")

    def save_accounts(self):
        with self._lock:
            data = []
            for b in self.bots.values():
                data.append({
                    "nickname": b.nickname,
                    "password": b.password,
                    "server": b.server_name,
                    "task": b.default_task_name or (b.active_task.name if b.active_task else "lvl"),
                    "is_banned": b.is_banned,
                    "proxy": b.proxy,
                    "lvl": b.level_str,
                    "money": b.money,
                    "target_lvl": b.target_lvl,
                })

        try:
            tmp_file = ACCOUNTS_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, ACCOUNTS_FILE)
        except Exception as e:
            print(f"[!] Ошибка сохранения {ACCOUNTS_FILE}: {e}")

    def notify_users(self, text: str):
        if hasattr(self, "tg_controller") and self.tg_controller:
            try:
                self.tg_controller.broadcast(text)
            except Exception as e:
                print(f"[!] Ошибка отправки уведомления в TG: {e}")

    def add_bot(
        self,
        target_server: str,
        nickname: str,
        password: str,
        auto_regen_nick: bool = False,
        default_task: str = "lvl",
        proxy: str = "",
    ) -> BotInstance | None:
        if not password or not password.strip():
            print("[-] Ошибка: Пароль обязателен для каждого аккаунта!")
            return None

        password = password.strip()

        try:
            srv = br_servers.find_server(target_server)
        except Exception as e:
            print(f"[!] Ошибка загрузки списка серверов: {e}")
            return None

        if not srv:
            print(f"[-] Сервер '{target_server}' не найден.")
            return None

        ip = srv["ip"]
        port = int(srv["port"])
        clean_name = srv.get("firstname") or srv.get("name", "").replace("BLACK RUSSIA | ", "").strip()
        clean_name = clean_name.upper()

        with self._lock:
            bot_id = self._next_id
            self._next_id += 1
            bot = BotInstance(
                bot_id=bot_id,
                host=ip,
                port=port,
                nickname=nickname,
                password=password,
                server_name=clean_name,
                auto_regen_nick=auto_regen_nick,
                manager=self,
                is_banned=False,
                default_task=default_task,
                proxy=proxy,
            )
            self.bots[bot_id] = bot
            if self.active_bot_id is None:
                self.active_bot_id = bot_id

        self.save_accounts()
        bot.start()
        if default_task:
            bot.set_task(default_task)

        px_label = f" [Прокси: {proxy}]" if proxy else " [Свой IP]"
        print(f"[+] Бот #{bot_id} ({nickname}) запущен на сервере {clean_name} ({ip}:{port}){px_label}")
        return bot

    def generate_random_nickname(self) -> str:
        names_file = os.path.join(os.path.dirname(__file__), "names.txt")
        surnames_file = os.path.join(os.path.dirname(__file__), "surnames.txt")

        if not hasattr(self, "_cached_names") or not self._cached_names:
            self._cached_names = []
            if os.path.exists(names_file):
                with open(names_file, "r", encoding="utf-8", errors="ignore") as f:
                    self._cached_names = [line.strip() for line in f if line.strip()]
            if not self._cached_names:
                self._cached_names = ["Alex", "Dmitry", "Sergey", "Maxim", "Ivan", "Artem", "Nikita"]

        if not hasattr(self, "_cached_surnames") or not self._cached_surnames:
            self._cached_surnames = []
            if os.path.exists(surnames_file):
                with open(surnames_file, "r", encoding="utf-8", errors="ignore") as f:
                    self._cached_surnames = [line.strip() for line in f if line.strip()]
            if not self._cached_surnames:
                self._cached_surnames = ["Smith", "Ivanov", "Smirnov", "Kuznetsov", "Popov", "Sokolov"]

        if not hasattr(self, "_used_nicknames"):
            self._used_nicknames = set()

        with self._lock:
            current_active_nicks = {b.nickname for b in self.bots.values() if b.nickname}
            all_used = self._used_nicknames | current_active_nicks

        for _ in range(500):
            first_name = random.choice(self._cached_names)
            last_name = random.choice(self._cached_surnames)
            nick = f"{first_name}_{last_name}"
            if len(nick) > MAX_NICKNAME_LEN:
                nick = nick[:MAX_NICKNAME_LEN]
            if nick not in all_used:
                self._used_nicknames.add(nick)
                return nick

        suffix = random.randint(10, 99)
        base = f"{random.choice(self._cached_names)}_{random.choice(self._cached_surnames)}"
        nick = f"{base[:MAX_NICKNAME_LEN - 3]}_{suffix}"
        self._used_nicknames.add(nick)
        return nick

    def replace_bot_nickname(self, bot: BotInstance):
        if bot.stopping:
            return

        old_nick = bot.nickname
        new_nick = self.generate_random_nickname()
        bot.add_log(f"Ник {old_nick} занят/зарегистрирован! Новый ник: {new_nick}")
        print(f"[!] Бот #{bot.bot_id} [{old_nick}] -> ник занят. Замена на: {new_nick}")

        task_name = bot.default_task_name or (bot.active_task.name if bot.active_task else "lvl")
        srv_name = bot.server_name
        pwd = bot.password

        bot.stop()

        def _respawn():
            time.sleep(2.0)
            with self._lock:
                if bot.bot_id in self.bots:
                    del self.bots[bot.bot_id]

            new_b = self.add_bot(srv_name, new_nick, password=pwd, auto_regen_nick=True, default_task=task_name)
            self.save_accounts()

        threading.Thread(target=_respawn, daemon=True).start()

    def load_proxies_list(self) -> list[str]:
        """Загрузка списка прокси из proxies.txt (формат ip:port или ip:port:user:pass или socks5://...)"""
        if not os.path.exists(PROXIES_FILE):
            return []
        try:
            with open(PROXIES_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
            return lines
        except Exception:
            return []

    def add_bots_batch(
        self,
        target_server: str,
        count: int,
        password: str,
        default_task: str = "lvl",
        proxies: list[str] | None = None,
    ):
        if not password or not password.strip():
            print("[-] Ошибка: Пароль обязателен для создания ботов!")
            return []

        password = password.strip()
        created = []
        proxies_pool = list(proxies) if proxies else []

        for i in range(count):
            nick = self.generate_random_nickname()
            # Если переданы прокси, распределяем их (по кругу)
            assigned_proxy = ""
            if proxies_pool:
                assigned_proxy = proxies_pool[i % len(proxies_pool)]

            bot = self.add_bot(
                target_server,
                nick,
                password=password,
                auto_regen_nick=True,
                default_task=default_task,
                proxy=assigned_proxy,
            )
            if bot:
                created.append(bot)
            time.sleep(2.0)
        print(f"[+] Успешно создано и запущено {len(created)} ботов на сервере '{target_server}' с задачей '{default_task}'")
        return created

    def start_selected(self, bot_ids: list[int]):
        with self._lock:
            bots_to_start = [
                self.bots[bid] for bid in bot_ids
                if bid in self.bots and not self.bots[bid].is_banned and "В игре" not in self.bots[bid].status and "Подключение" not in self.bots[bid].status
            ]

        if not bots_to_start:
            print("[*] Нет выбранных ботов, готовых к запуску.")
            return []

        def _starter():
            for b in bots_to_start:
                if b.is_banned or b.stopping:
                    continue
                print(f"[+] Запуск бота #{b.bot_id} [{b.nickname}] на сервере {b.server_name}...")
                b.start()
                time.sleep(0.4)
            print(f"[+] Выбранные боты ({len(bots_to_start)}) запущены!")

        threading.Thread(target=_starter, daemon=True).start()
        return bots_to_start

    def start_all(self):
        with self._lock:
            bots_to_start = [
                b for b in self.bots.values()
                if not b.is_banned and "В игре" not in b.status and "Подключение" not in b.status
            ]

        if not bots_to_start:
            print("[*] Нет ботов, готовых к запуску (все уже в игре или забанены).")
            return

        print(f"[*] Запуск {len(bots_to_start)} ботов...")

        def _starter():
            for b in bots_to_start:
                if b.is_banned or b.stopping:
                    continue
                print(f"[+] Запуск бота #{b.bot_id} [{b.nickname}] на сервере {b.server_name}...")
                b.start()
                # Небольшая пауза между запусками ботов (0.3-0.5с), чтобы не вызывать лавину UDP
                time.sleep(0.4)
            print(f"[+] Все боты ({len(bots_to_start)}) запущены!")

        threading.Thread(target=_starter, daemon=True).start()

    def start_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        if bot.is_banned:
            print(f"[!] Бот #{bot_id} помечен как ЗАБАНЕН. Используйте /unban {bot_id} если бан снят.")
            return
        bot.start()
        print(f"[+] Бот #{bot_id} [{bot.nickname}] запускается...")

    def unban_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        bot.is_banned = False
        bot.status = "Отключен"
        bot.add_log("Статус бана снят пользователем")
        self.save_accounts()
        print(f"[+] Бот #{bot_id} [{bot.nickname}] разбанен! Теперь его можно запустить командой /start {bot_id}")

    def set_bot_proxy(self, bot_id: int, proxy_str: str) -> bool:
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                return False
            proxy_clean = proxy_str.strip()
            if proxy_clean.lower() in ("none", "0", "off", "no", "direct", "-"):
                bot.proxy = ""
            else:
                # Если в строке http:// или https://, предупреждаем/конвертируем в socks5 для UDP если протокол не указан
                if proxy_clean.startswith("http://"):
                    # Заменяем префикс на socks5://
                    proxy_clean = "socks5://" + proxy_clean[7:]
                elif proxy_clean.startswith("https://"):
                    proxy_clean = "socks5://" + proxy_clean[8:]
                bot.proxy = proxy_clean

        self.save_accounts()
        return True

    def delete_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
            if not bot:
                print(f"[-] Бот #{bot_id} не найден.")
                return
            bot.stop()
            del self.bots[bot_id]
        self.save_accounts()
        print(f"[*] Бот #{bot_id} [{bot.nickname}] удален из списка и accounts.json.")

    def _get_bot_location_name(self, bot: BotInstance) -> str:
        """Определяет понятное название текущей локации бота по координатам"""
        if not bot.api or "В игре" not in bot.status:
            return ""
        try:
            pos = bot.api.getbotposition()
            if not pos or len(pos) < 3 or pos[0] == 0.0:
                return "Спавн"
            x, y, z = pos[0], pos[1], pos[2]

            # База координат локаций области Black Russia
            locations = [
                ("Вокзал Арзамас", 2125.0, -1145.0, 180.0),
                ("Центр Арзамаса", 2250.0, -1350.0, 250.0),
                ("Больница Арзамас", 2040.0, -1410.0, 150.0),
                ("Вокзал Батырево", 1060.0, 1260.0, 150.0),
                ("Военкомат / Батырево", 1100.0, 1340.0, 200.0),
                ("Шахта (Карьер)", 815.0, 860.0, 220.0),
                ("Автошкола Лыткарино", 2065.0, -2385.0, 120.0),
                ("Завод (Арзамас - Нижегородск)", 2450.0, -450.0, 250.0),
                ("Вокзал Лыткарино", 2800.0, -2320.0, 180.0),
                ("г. Нижегородск", 2850.0, 500.0, 400.0),
                ("Вокзал Южный", -2430.0, 2350.0, 180.0),
                ("ГИБДД Южный", -2550.0, 2280.0, 200.0),
                ("Ферма", 120.0, 250.0, 220.0),
                ("Эдово", 2300.0, 2400.0, 300.0),
                ("Бусаево", 1550.0, 350.0, 250.0),
                ("Корякино", 1850.0, -450.0, 250.0),
            ]

            for name, lx, ly, radius in locations:
                dist = ((x - lx)**2 + (y - ly)**2)**0.5
                if dist <= radius:
                    return name

            # Если конкретной точки нет, определяем по регионам карты
            if x > 1500 and y < -800 and y > -1800:
                return "г. Арзамас"
            elif x > 1700 and y < -2000:
                return "г. Лыткарино"
            elif x > 600 and x < 1400 and y > 600 and y < 1600:
                return "пгт. Батырево"
            elif x < -1800 and y > 1800:
                return "г. Южный"
            elif x > 1800 and y > 1800:
                return "пгт. Эдово"

            return f"Карта ({int(x)}, {int(y)})"
        except Exception:
            return "В игре"

    def list_bots(self):
        print("\n" + "=" * 124)
        print(f"{'ID':<4} {'Никнейм':<18} {'Сервер':<10} {'ЛВЛ':<12} {'Задача':<13} {'Локация':<20} {'Статус':<22} {'Прокси':<15}")
        print("-" * 124)
        with self._lock:
            if not self.bots:
                print("   (нет добавленных ботов)")
            for bid, b in self.bots.items():
                cur_marker = "* " if bid == self.active_bot_id else "  "
                online_str = b.status
                if b.connected_time and "В игре" in b.status:
                    uptime = int(time.time() - b.connected_time)
                    m, s = divmod(uptime, 60)
                    h, m = divmod(m, 60)
                    online_str += f" ({h:02d}:{m:02d})"
                task_name = b.active_task.name if b.active_task else (b.default_task_name or "Ручной")
                # Укорачиваем длинные названия задач
                short_task = task_name.split()[0] if task_name else "Ручной"
                if "качка" in task_name.lower():
                    short_task = "Качка LVL"
                elif "шахт" in task_name.lower():
                    short_task = "Шахтер"
                elif "завод" in task_name.lower():
                    short_task = "Завод"
                elif "автобус" in task_name.lower():
                    short_task = "Автобусник"
                elif "квест" in task_name.lower():
                    short_task = "Дядя Слава"

                loc_name = self._get_bot_location_name(b) or ("-" if "В игре" not in b.status else "Загрузка...")
                lvl_disp = b.level_str or "-"
                px_disp = b.proxy.split("@")[-1] if b.proxy else "Свой IP"
                if len(px_disp) > 14:
                    px_disp = px_disp[:12] + ".."
                print(f"{cur_marker}{bid:<2} {b.nickname:<18} {b.server_name[:9]:<10} {lvl_disp:<12} {short_task:<13} {loc_name:<20} {online_str:<22} {px_disp:<15}")
        print("=" * 124)
        if self.active_bot_id is not None:
            active_bot = self.bots.get(self.active_bot_id)
            if active_bot:
                print(f"[*] Активный бот для команд: #{self.active_bot_id} [{active_bot.nickname}]\n")

    def show_logs(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return

        print(f"\n--- Последние логи бота #{bot_id} [{bot.nickname}] ({len(bot.logs)} записей) ---")
        if not bot.logs:
            print("   (логов пока нет)")
        else:
            for line in list(bot.logs):
                print(line)
        print("--- Конец логов ---\n")

    def switch_active(self, bot_id: int):
        with self._lock:
            if bot_id not in self.bots:
                print(f"[-] Бот #{bot_id} не существует.")
                return False
            self.active_bot_id = bot_id
            bot = self.bots[bot_id]
        print(f"[+] Выбран бот #{bot_id} [{bot.nickname}] на сервере {bot.server_name}")
        return True

    def stop_bot(self, bot_id: int):
        with self._lock:
            bot = self.bots.get(bot_id)
        if not bot:
            print(f"[-] Бот #{bot_id} не найден.")
            return
        bot.stop()
        print(f"[*] Бот #{bot_id} [{bot.nickname}] остановлен.")

    def stop_all(self):
        with self._lock:
            bots_list = list(self.bots.values())
        for b in bots_list:
            b.stop()


def print_help():
    print("""
====================== УПРАВЛЕНИЕ МУЛЬТИБОТОМ ======================
  /menu             - Главное меню управления аккаунтами и задачами
  /list             - Список всех ботов, серверов, паролей и статусов
  /startall         - Запустить всех доступных ботов (Кнопка 'Старт всех')
  /start <ID|1,2,3> - Запустить бота или нескольких через запятую
  /stopall          - Остановить всех ботов
  /stop <ID|1,2,3>  - Остановить бота или нескольких через запятую
  /batch <сервер> <кол-во> <пароль> - Создать пачку ботов с авто-никами на качку LVL
  /add <сервер> <ник> <пароль>      - Добавить аккаунт вручную
  /task <lvl|quest|mine|factory|bus|stop> [ID] - Назначить задачу боту
  /switch <ID>      - Выбрать активного бота для консольных команд
  /logs [ID]        - Посмотреть последние логи бота
  /unban <ID>       - Снять статус бана с аккаунта
  /del <ID>         - Удалить бота из списка и accounts.json
  /stream           - Вкл/выкл живой вывод логов активного бота в консоль
  /q                - Выход и остановка всех ботов

--- Команды для активного бота ---
  /chat <текст>     - Отправить сообщение в чат
  /<команда>        - Любая серверная команда (например /time, /stats, /mm)
  /dialog <id> <кнопка> [список] [текст] - Ответить в диалог
====================================================================
""")


def interactive_menu(manager: BotManager):
    manager.stream_active_logs = False
    try:
        while True:
            print("\n============ МЕНЮ УПРАВЛЕНИЯ ============")
            print("1. Показать список всех аккаунтов")
            print("2. Добавить один аккаунт вручную (Пароль обязателен)")
            print("3. Создать пачку ботов (Авто-генерация ников + качка LVL)")
            print("4. ЗАПУСТИТЬ ВСЕХ БОТОВ (Кнопка 'Старт всех')")
            print("5. Остановить всех ботов")
            print("6. Запустить режим бота (Качка LVL / Квесты / Шахта)")
            print("7. Посмотреть логи аккаунта")
            print("8. Переключить активный аккаунт")
            print("9. Остановить одного бота")
            print("10. Снять статус бана с аккаунта")
            print("11. Удалить аккаунт из списка")
            print("12. Управление прокси (добавить / посмотреть / привязать)")
            print("0. Вернуться в консоль")
            print("=========================================")
            choice = input("Выберите пункт (0-12): ").strip()

            if choice == "0":
                break
            elif choice == "1":
                manager.list_bots()
            elif choice == "2":
                srv = input("Сервер (название или номер, например tver): ").strip()
                if not srv:
                    print("Отменено.")
                    continue
                nick = input("Никнейм (Name_Surname): ").strip()
                if not nick:
                    print("[-] Ошибка: Ник не может быть пустым.")
                    continue
                while True:
                    pwd = input("Пароль аккаунта (обязательно): ").strip()
                    if pwd:
                        break
                    print("[-] Ошибка: Пароль обязателен!")
                px_choice = input("Прокси (Enter для собственного IP, или ip:port / ip:port:user:pass): ").strip()
                manager.add_bot(srv, nick, pwd, proxy=px_choice)
            elif choice == "3":
                srv = input("Сервер (название или номер, например tver): ").strip()
                if not srv:
                    print("Отменено.")
                    continue
                cnt_str = input("Количество ботов: ").strip()
                if not cnt_str.isdigit() or int(cnt_str) <= 0:
                    print("[-] Ошибка: Неверное количество.")
                    continue
                count = int(cnt_str)
                while True:
                    pwd = input("Пароль для всех создаваемых аккаунтов (обязательно): ").strip()
                    if pwd:
                        break
                    print("[-] Ошибка: Пароль обязателен!")
                
                # Выбор прокси для пачки
                px_list = manager.load_proxies_list()
                selected_proxies = None
                if px_list:
                    print(f"\n[i] Найдено {len(px_list)} прокси в {os.path.basename(PROXIES_FILE)}.")
                    use_px = input(f"Использовать прокси из файла для распределения? (y/n, по умолч. y): ").strip().lower()
                    if use_px != "n":
                        selected_proxies = px_list
                else:
                    manual_px = input("Прокси для пачки (Enter - свой IP, или введите через запятую/один прокси): ").strip()
                    if manual_px:
                        selected_proxies = [p.strip() for p in manual_px.split(",") if p.strip()]

                manager.add_bots_batch(srv, count, password=pwd, default_task="lvl", proxies=selected_proxies)
            elif choice == "4":
                manager.start_all()
            elif choice == "5":
                manager.stop_all()
                print("[*] Все боты остановлены.")
            elif choice == "6":
                manager.list_bots()
                b_str = input("Введите ID бота для смены режима: ").strip()
                if b_str.isdigit():
                    bid = int(b_str)
                    with manager._lock:
                        target_b = manager.bots.get(bid)
                    if not target_b:
                        print(f"[-] Бот #{bid} не найден.")
                        continue
                    print("\nВыберите режим:")
                    print("  1. Качка LVL (Anti-AFK, микро-движения, сбор наград, скип диалогов)")
                    print("  2. Квесты Дяди Славы (авто-диалоги, маркеры, посадка в авто)")
                    print("  3. Бот Шахтер (аренда скутера, добыча руды с ползунком, склад)")
                    print("  4. Бот Завод (аренда скутера, сырье, станок с ползунком, авто-перезабор при браке)")
                    print("  5. Бот Автобусник (проверка/сдача на кат. D, рейс в Арзамасе, бессмертие, объезд)")
                    print("  6. Выключить авто-режим (Ручное управление)")
                    t_choice = input("Режим (1-6): ").strip()
                    if t_choice == "1":
                        res = target_b.set_task("lvl")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "2":
                        res = target_b.set_task("quest")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "3":
                        res = target_b.set_task("mine")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "4":
                        res = target_b.set_task("factory")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "5":
                        res = target_b.set_task("bus")
                        print(f"[+] Бот #{bid}: {res}")
                    elif t_choice == "6":
                        res = target_b.set_task("stop")
                        print(f"[*] Бот #{bid}: {res}")
            elif choice == "7":
                manager.list_bots()
                b_str = input("Введите ID бота для просмотра логов: ").strip()
                if b_str.isdigit():
                    manager.show_logs(int(b_str))
            elif choice == "8":
                manager.list_bots()
                b_str = input("Введите ID бота, на которого переключиться: ").strip()
                if b_str.isdigit():
                    manager.switch_active(int(b_str))
            elif choice == "9":
                manager.list_bots()
                b_str = input("Введите ID бота для остановки: ").strip()
                if b_str.isdigit():
                    manager.stop_bot(int(b_str))
            elif choice == "10":
                manager.list_bots()
                b_str = input("Введите ID бота для снятия бана: ").strip()
                if b_str.isdigit():
                    manager.unban_bot(int(b_str))
            elif choice == "11":
                manager.list_bots()
                b_str = input("Введите ID бота для удаления: ").strip()
                if b_str.isdigit():
                    manager.delete_bot(int(b_str))
            elif choice == "12":
                print("\n--- УПРАВЛЕНИЕ ПРОКСИ ---")
                print("1. Посмотреть текущие прокси из proxies.txt")
                print("2. Добавить прокси в proxies.txt")
                print("3. Привязать / изменить прокси у бота")
                p_sub = input("Выберите (1-3): ").strip()
                if p_sub == "1":
                    cur_px = manager.load_proxies_list()
                    if cur_px:
                        print(f"Список прокси ({len(cur_px)}):")
                        for idx, px in enumerate(cur_px, 1):
                            print(f"  {idx}. {px}")
                    else:
                        print(f"Файл {os.path.basename(PROXIES_FILE)} пуст или не существует.")
                elif p_sub == "2":
                    print("Формат: ip:port или ip:port:user:pass (или socks5://...)")
                    new_p = input("Введите прокси: ").strip()
                    if new_p:
                        with open(PROXIES_FILE, "a", encoding="utf-8") as pf:
                            pf.write(new_p + "\n")
                        print("[+] Прокси сохранен в proxies.txt")
                elif p_sub == "3":
                    manager.list_bots()
                    b_str = input("Введите ID бота: ").strip()
                    if b_str.isdigit():
                        bid = int(b_str)
                        with manager._lock:
                            target_b = manager.bots.get(bid)
                        if target_b:
                            new_proxy = input(f"Введите новый прокси для #{bid} (или 'none'/'0' для сброса на свой IP): ").strip()
                            if new_proxy.lower() in ("none", "0", "off", "no"):
                                target_b.proxy = ""
                                print(f"[+] У бота #{bid} сброшен прокси на собственный IP.")
                            else:
                                target_b.proxy = new_proxy
                                print(f"[+] Боту #{bid} назначен прокси: {new_proxy}")
                            manager.save_accounts()
                        else:
                            print(f"[-] Бот #{bid} не найден.")
    finally:
        manager.stream_active_logs = True
        print("[*] Вы вернулись в консоль управления. Введите /help для подсказки.")


def main():
    print("=" * 70)
    print("   Black Russia Headless RakSAMP Bot Client [Multi-Account]   ")
    print("=" * 70)

    # Разбор аргументов для Telegram Bot
    tg_token = None
    tg_admin = None
    filtered_args = []
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("--tg", "--telegram", "--token") and i + 1 < len(sys.argv):
            tg_token = sys.argv[i + 1]
            i += 2
        elif arg.startswith("--tg="):
            tg_token = arg.split("=", 1)[1]
            i += 1
        elif arg in ("--admin", "--tg-admin") and i + 1 < len(sys.argv):
            try:
                tg_admin = int(sys.argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif arg.startswith("--admin="):
            try:
                tg_admin = int(arg.split("=", 1)[1])
            except ValueError:
                pass
            i += 1
        else:
            filtered_args.append(arg)
            i += 1

    # Также проверяем переменную окружения TG_BOT_TOKEN
    if not tg_token:
        tg_token = os.environ.get("TG_BOT_TOKEN")
    if not tg_admin and os.environ.get("TG_ADMIN_ID"):
        try:
            tg_admin = int(os.environ.get("TG_ADMIN_ID"))
        except ValueError:
            pass

    manager = BotManager()

    if tg_token:
        print(f"[*] Запуск Telegram бота управления...")
        tg_bot = TelegramBotController(tg_token, manager, admin_id=tg_admin)
        manager.tg_controller = tg_bot
        tg_bot.start()

    if len(filtered_args) >= 3:
        target_server = filtered_args[0]
        nickname = filtered_args[1]
        password = filtered_args[2]
        manager.add_bot(target_server, nickname, password=password)
    elif len(filtered_args) in (1, 2):
        print("[-] Ошибка: Для запуска через аргументы пароль обязателен!")
        print("Использование: python3 bot.py <сервер> <никнейм> <пароль> [--tg ТОКЕН] [--admin ID]")
    else:
        if manager.bots:
            print(f"\n[i] Найдено {len(manager.bots)} сохраненных аккаунтов из {os.path.basename(ACCOUNTS_FILE)}.")
            manager.list_bots()
            print("Введите /startall чтобы запустить всех, или /menu для перехода в меню.\n")
        else:
            print("\n[i] Первый запуск. Добавьте аккаунты через меню.")
            interactive_menu(manager)

    print_help()

    # Если запущен в фоне с Telegram ботом (например nohup / daemon без интерактивного TTY)
    if tg_token and not sys.stdin.isatty():
        print("[*] Режим фоновой работы с Telegram-ботом активен. Ожидание команд из Telegram...")
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        print("\n[*] Завершение работы всех ботов...")
        manager.stop_all()
        emitter.shutdown(wait=False)
        return

    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not line:
            continue

        if line in ("/q", "exit", "quit"):
            break

        elif line in ("/menu", "menu"):
            interactive_menu(manager)

        elif line in ("/list", "list"):
            manager.list_bots()

        elif line in ("/startall", "/runall") or line == "/start all":
            manager.start_all()

        elif line.startswith("/start"):
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                arg = parts[1].strip()
                if arg.lower() == "all":
                    manager.start_all()
                elif "," in arg:
                    # Запуск нескольких ботов через запятую, например /start 1,2,5
                    ids = []
                    for item in arg.split(","):
                        item = item.strip()
                        if item.isdigit():
                            ids.append(int(item))
                    if ids:
                        print(f"[*] Запуск выбранных ботов: {ids}...")
                        for bid in ids:
                            manager.start_bot(bid)
                    else:
                        print("[-] Не указаны корректные ID ботов через запятую.")
                elif arg.isdigit():
                    manager.start_bot(int(arg))
                else:
                    print("Использование: /start <ID бота>, /start 1,2,3 или /startall")
            else:
                print("Использование: /start <ID бота>, /start 1,2,3 или /startall")

        elif line in ("/stopall",):
            manager.stop_all()
            print("[*] Все боты остановлены.")

        elif line in ("/help", "help"):
            print_help()

        elif line.startswith("/add"):
            parts = line.split(maxsplit=4)
            if len(parts) >= 4 and parts[3].strip():
                srv = parts[1]
                nick = parts[2]
                pwd = parts[3].strip()
                px = parts[4].strip() if len(parts) >= 5 else ""
                manager.add_bot(srv, nick, pwd, proxy=px)
            else:
                print("[-] Ошибка: Пароль обязателен!")
                print("Использование: /add <сервер> <никнейм> <пароль> [прокси]")

        elif line.startswith("/switch"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.switch_active(int(parts[1]))
            else:
                print("Использование: /switch <ID бота>")

        elif line.startswith("/batch"):
            parts = line.split()
            if len(parts) >= 4 and parts[2].isdigit() and parts[3].strip():
                s_name = parts[1]
                b_count = int(parts[2])
                b_pwd = parts[3].strip()
                manager.add_bots_batch(s_name, b_count, password=b_pwd, default_task="lvl")
            else:
                print("[-] Ошибка: Пароль обязателен!")
                print("Использование: /batch <сервер> <кол-во> <пароль>")

        elif line.startswith("/task"):
            parts = line.split()
            if len(parts) >= 2:
                t_name = parts[1].lower()
                target_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else manager.active_bot_id
                if target_id is not None:
                    with manager._lock:
                        target_b = manager.bots.get(target_id)
                    if target_b:
                        res = target_b.set_task(t_name)
                        print(f"[+] Бот #{target_id}: {res}")
                    else:
                        print(f"[-] Бот #{target_id} не найден.")
                else:
                    print("[-] Укажите ID бота: /task <lvl|quest|mine|stop> [ID]")
            else:
                print("Использование: /task <lvl|quest|mine|stop> [ID бота]")

        elif line.startswith("/logs"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.show_logs(int(parts[1]))
            elif manager.active_bot_id is not None:
                manager.show_logs(manager.active_bot_id)
            else:
                print("Нет активного бота. Укажите ID: /logs <ID>")

        elif line == "/stream":
            manager.stream_active_logs = not manager.stream_active_logs
            st = "ВКЛЮЧЕН" if manager.stream_active_logs else "ВЫКЛЮЧЕН"
            print(f"[*] Живой стриминг логов в консоль: {st}")

        elif line.startswith("/unban"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.unban_bot(int(parts[1]))
            else:
                print("Использование: /unban <ID бота>")

        elif line.startswith(("/del", "/delete")):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                manager.delete_bot(int(parts[1]))
            else:
                print("Использование: /del <ID бота>")

        elif line.startswith("/stop"):
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                arg = parts[1].strip()
                if arg.lower() == "all":
                    manager.stop_all()
                    print("[*] Все боты остановлены.")
                elif "," in arg:
                    ids = []
                    for item in arg.split(","):
                        item = item.strip()
                        if item.isdigit():
                            ids.append(int(item))
                    if ids:
                        for bid in ids:
                            manager.stop_bot(bid)
                    else:
                        print("[-] Не указаны корректные ID ботов через запятую.")
                elif arg.isdigit():
                    manager.stop_bot(int(arg))
                else:
                    print("Использование: /stop <ID бота> или /stop 1,2,3")
            else:
                print("Использование: /stop <ID бота> или /stop 1,2,3")

        elif line.startswith("/dialog"):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if not active or not active.session:
                print("[-] Нет активного подключенного бота для отправки диалога.")
                continue
            parts = line.split(maxsplit=4)
            if len(parts) >= 3:
                try:
                    d_id = int(parts[1])
                    btn = int(parts[2])
                    list_item = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                    txt = parts[4] if len(parts) > 4 else (parts[3] if len(parts) > 3 and not parts[3].isdigit() else "")
                    from samp.packets import PacketBuilder
                    active.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", d_id, btn, list_item, txt),
                        rpc_name="dialog_response",
                    )
                    active.add_log(f"Ответ в диалог #{d_id}: кнопка={btn}, элемент={list_item}, текст='{txt}'")
                except Exception as e:
                    print(f"[-] Ошибка отправки диалога: {e}")
            else:
                print("Использование: /dialog <dialog_id> <кнопка: 0/1> [номер_строки: 0] [текст]")

        elif line.startswith("/chat "):
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if active:
                active.send_chat(line[6:])
            else:
                print("[-] Нет активного бота.")

        else:
            with manager._lock:
                active = manager.bots.get(manager.active_bot_id) if manager.active_bot_id else None
            if active:
                active.send_chat(line)
            else:
                print("[-] Нет активного бота. Выберите бота через /switch <ID> или введите /menu")

    print("\n[*] Завершение работы всех ботов...")
    manager.stop_all()
    emitter.shutdown(wait=False)


if __name__ == "__main__":
    main()
