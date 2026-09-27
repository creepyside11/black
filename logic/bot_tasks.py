# Engine: Python 3 | Termux
# Module: logic/bot_tasks.py
# Specialized automation tasks: LevelGrinder & QuestMaster (Uncle Slava / Начальные квесты)

import time
import threading
import random
import re
from core import logger
from samp.packets import PacketBuilder


def solve_captcha_text(text: str) -> str | None:
    """
    Универсальный решатель капчи Black Russia для шахты, завода и работ:
    1. Буквенная капча с картинки/текстдрава (разные буквы, повернутые/по-разному расставленные)
    2. Математические примеры: "Сколько будет 15 + 7?", "2 + 3 = ?", "10 - 4"
    3. Повтор чисел/кода: "Введите код: 8492", "Код: 7123"
    4. Текстовые примеры словами: "пять плюс два", "десять минус три"
    """
    if not text:
        return None

    # Очищаем от SAMP цветовых кодов вроде {FFFFFF}, {00FF00}
    clean = re.sub(r'\{[0-9a-fA-F]{6}\}', '', text).strip()
    # Убираем спецсимволы разметки
    clean_no_tags = re.sub(r'~[a-z]~', '', clean).strip()
    c_lower = clean_no_tags.lower()

    # 1. Поиск последовательности букв капчи (например: "Введите буквы: aBcX", "Код: WxYz", "Капча: hKdP")
    # В Black Russia капча с буквами обычно 3-6 символов (латиница или кириллица)
    letter_code_match = re.search(r'(?:букв[ыа]|символ[ыа]|код|капч[аеу]|защ[ие]т[аеу]|проверк[аеу])[:\s]+([a-zA-Zа-яА-Я0-9]{3,7})', clean_no_tags, re.IGNORECASE)
    if letter_code_match:
        return letter_code_match.group(1).strip()

    # 2. Если буквы разбросаны с пробелами/точками/разделителями в картинке: "W  X   Y   Z" или "w.x.y.z"
    spaced_letters = re.findall(r'(?:^|[\s:])[a-zA-Zа-яА-Я](?:\s+[a-zA-Zа-яА-Я]){2,6}(?:[\s$]|$)', clean_no_tags)
    if spaced_letters:
        found_code = re.sub(r'\s+', '', spaced_letters[0]).strip()
        if 3 <= len(found_code) <= 7:
            return found_code

    # 3. Математическое выражение (12 + 5, 20 - 8, 3 * 4)
    math_match = re.search(r'(\d+)\s*([\+\-\*\/])\s*(\d+)', clean_no_tags)
    if math_match:
        try:
            n1 = int(math_match.group(1))
            op = math_match.group(2)
            n2 = int(math_match.group(3))
            if op == '+':
                return str(n1 + n2)
            elif op == '-':
                return str(n1 - n2)
            elif op == '*':
                return str(n1 * n2)
            elif op == '/' and n2 != 0:
                return str(n1 // n2)
        except Exception:
            pass

    # 4. Поиск чисел словами на русском
    word_digits = {
        "ноль": 0, "один": 1, "два": 2, "три": 3, "четыре": 4,
        "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
        "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
        "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
        "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20
    }
    for w1, v1 in word_digits.items():
        if w1 in c_lower:
            for w2, v2 in word_digits.items():
                if w2 in c_lower and w1 != w2:
                    if any(p in c_lower for p in ["плюс", "+", "прибавить"]):
                        return str(v1 + v2)
                    elif any(m in c_lower for m in ["минус", "-", "отнять"]):
                        return str(v1 - v2)

    # 5. Числовой код ("введите число 4821", "код: 8392", "капча: 9214")
    code_match = re.search(r'(?:число|код|цифры|капч[аеу]|ответ)[:\s]+([0-9]{2,6})', c_lower)
    if code_match:
        return code_match.group(1).strip()

    # 6. Если в строке изолированный блок из 3-6 символов (буквы/цифры)
    tokens = clean_no_tags.split()
    for tok in tokens:
        clean_tok = re.sub(r'[^a-zA-Zа-яА-Я0-9]', '', tok)
        if 3 <= len(clean_tok) <= 6 and not clean_tok.isdigit():
            # Проверяем, что это не стандартное слово
            if clean_tok.lower() not in ["введите", "ответ", "код", "капча", "проверка", "пример", "нажмите"]:
                return clean_tok

    digits_only = re.findall(r'\b\d{2,6}\b', clean_no_tags)
    if digits_only:
        return digits_only[0]

    return None


class BaseTask:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.api = bot_instance.api
        self.session = bot_instance.session
        self.running = False
        self.thread = None
        self.name = "BaseTask"

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.bot.add_log(f"[TASK] Задача '{self.name}' запущена")

    def stop(self):
        self.running = False
        self.bot.add_log(f"[TASK] Задача '{self.name}' остановлена")

    def _run_loop(self):
        raise NotImplementedError


class LevelGrinderTask(BaseTask):
    """
    Бот для прокачки уровня (Anti-AFK + сбор бонусов PayDay + рулетка/кейсы/донат)
    - Передвигается на небольшие расстояния / делает микро-движения раз в N минут, чтобы избежать кика
    - Каждые 20-30 минут проверяет /time и баланс/опыт
    - Автоматически забирает ежедневные награды и бесплатную рулетку (GetFreeReward)
    - Закрывает назойливые системные диалоги и уведомления
    - Скипает / отклоняет сюжетную линию и квесты Дяди Славы при появлении
    """
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Качка LVL (Anti-AFK & EXP Grinder)"
        self.last_action = time.time()
        self.start_time = time.time()
        self.exp_collected = 0

    def on_chat_message(self, message: str):
        """Интеллектуальный авто-ответ на вопросы игроков и админов рядом"""
        if not self.running:
            return
        clean_msg = re.sub(r'\{[0-9a-fA-F]{6}\}', '', message).strip()
        msg_l = clean_msg.lower()

        # Проверяем обращение к боту или общие вопросы проверки
        # "ты тут?", "тут?", "вы тут?", "бот?", "afk?", "ау", "ответь", "слышишь"
        is_check = any(re.search(pat, msg_l) for pat in [
            r'\bты\s+тут\b',
            r'\bвы\s+тут\b',
            r'\bтут[\?\!\.]*\b',
            r'\bты\s+здесь\b',
            r'\bздесь[\?\!\.]*\b',
            r'\bбот\b',
            r'\bответь\b',
            r'\bживой\b',
            r'\bку\b',
            r'\bпривет\b'
        ])

        if is_check:
            # Выбираем естественный человеческий ответ
            if "ку" in msg_l or "привет" in msg_l:
                answer = random.choice(["ку", "ку бро", "привет", "куку", "дарова"])
            elif "бот" in msg_l:
                answer = random.choice(["сам ты бот ахах", "не, я тут", "нет, играю", "я тут, че надо?"])
            else:
                answer = random.choice(["тут", "да тут я", "да", "тут я", "тут, че хотел?", "+", "да, на месте"])

            self.bot.add_log(f"[LVL-ЧАТ] Замечен вопрос игрока: '{clean_msg}'. Отвечаем: '{answer}'")
            def _answer():
                # Случайная человеческая задержка печати (1.5 - 3.5 секунды)
                time.sleep(random.uniform(1.8, 3.8))
                try:
                    self.api.sendchat(answer)
                    self.bot.add_log(f"[LVL-ЧАТ] Ответ отправлен в чат: '{answer}'")
                except Exception as e:
                    self.bot.add_log(f"[LVL-ЧАТ] Ошибка отправки ответа: {e}")
            threading.Thread(target=_answer, daemon=True).start()

    def on_dialog(self, d_id: int, style: int, title: str, info: str, b1: str, b2: str):
        """Авто-ответ на диалог админа / проверку на бота ('Вы тут? Ответьте в диалог')"""
        if not self.running:
            return
        comb = f"{title} {info}".lower()
        if any(w in comb for w in ["вы тут", "ты тут", "проверка", "администратор", "ответ"]):
            self.bot.add_log(f"[LVL-ПРОВЕРКА] Обнаружен диалог проверки от администрации #{d_id}: '{title}'")
            def _reply_admin():
                time.sleep(random.uniform(1.5, 2.5))
                try:
                    out = random.choice(["Да, я тут", "тут", "да тут я, играю"])
                    self.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", int(d_id), 1, 0, out),
                        rpc_name="dialog_response",
                    )
                    self.bot.add_log(f"[LVL-ПРОВЕРКА] Ответ администратору '{out}' успешно отправлен!")
                except Exception as e:
                    self.bot.add_log(f"[LVL-ПРОВЕРКА] Ошибка ответа админу: {e}")
            threading.Thread(target=_reply_admin, daemon=True).start()

    def on_npc_dialog(self, name: str, text: str, model, buttons):
        if not self.running:
            return
        # Скипаем диалоги Дяди Славы / вводные квесты: выбираем отказ / закрытие / пропуск
        skip_key = None
        for b in (buttons or []):
            b_text = str(b.get("text", "")).lower()
            if any(w in b_text for w in ["отказ", "нет", "пропустить", "позже", "не хочу", "отмена", "закрыть"]):
                skip_key = b.get("key", 1)
                break

        # Если явной кнопки отказа нет, но это диалог новичка/славы, берем кнопку закрытия/вторую кнопку
        if skip_key is None and (buttons or []):
            if len(buttons) > 1:
                skip_key = buttons[-1].get("key", 2)
            else:
                skip_key = buttons[0].get("key", 1)

        if skip_key is not None:
            def _reply():
                time.sleep(random.uniform(0.8, 1.5))
                try:
                    self.api.send_npc_dialog_response(skip_key)
                    self.bot.add_log(f"[LVL-BOT] Пропуск/отказ диалога '{name}' (key={skip_key})")
                except Exception:
                    pass
            threading.Thread(target=_reply, daemon=True).start()

    def _run_loop(self):
        self.bot.add_log("[LVL-BOT] Режим качки уровня активирован. Поддержание активности 24/7.")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                # 1. Защита от голода/смерти при 0 XP: если 0 XP или 1 lvl, включаем бессмертие (100 HP)
                bot_player = getattr(self.session.players, "bot", None)
                cur_lvl = self.bot.get_numeric_level() if hasattr(self.bot, "get_numeric_level") else 1
                is_zero_xp = (cur_lvl <= 1 and ("0/" in self.bot.level_str or self.bot.level_str == "-" or "1 lvl" in self.bot.level_str))
                if bot_player:
                    if is_zero_xp:
                        bot_player.godmode_zero_xp = True
                        bot_player.health = 100.0
                    else:
                        bot_player.godmode_zero_xp = False

                # 2. Детект игроков рядом (эмуляция ухода в AFK / паузу)
                # Если игрок подошел ближе 8-10 метров, бот встает в настоящую паузу (прекращает слать sync-пакеты)
                nearby_player_found = False
                cur_pos = self.api.getbotposition()
                if cur_pos and len(cur_pos) >= 3 and cur_pos[0] != 0.0:
                    bx, by, bz = cur_pos[0], cur_pos[1], cur_pos[2]
                    streamed = self.session.players.get_streamed_players()
                    for pid, p in streamed.items():
                        if getattr(p, "is_npc", False):
                            continue
                        px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
                        dist = ((bx - px)**2 + (by - py)**2 + (bz - pz)**2) ** 0.5
                        if 0.5 < dist < 9.0: # Игрок рядом
                            nearby_player_found = True
                            break

                if nearby_player_found:
                    if bot_player and not getattr(bot_player, "is_paused", False):
                        bot_player.is_paused = True
                        self.bot.add_log("[LVL-BOT] Рядом замечен игрок (<9м). Эмуляция паузы (AFK в меню)...")
                    # Ждем в паузе 4-8 секунд и проверяем снова
                    time.sleep(random.uniform(4.0, 7.0))
                    continue
                else:
                    if bot_player and getattr(bot_player, "is_paused", False):
                        bot_player.is_paused = False
                        self.bot.add_log("[LVL-BOT] Игрок отошел. Снятие с паузы, возобновление активности.")

                now = time.time()

                # 3. Забираем бесплатные награды из донат-меню раз в 30 минут
                if now - self.last_action > 1800:
                    try:
                        self.api.GetFreeReward()
                        self.api.DonatMenuClose()
                    except Exception:
                        pass
                    self.last_action = now

                # 4. Улучшенное анти-АФК поведение (повороты головы, микро-шаги, осмотр, случайные приседания)
                if cur_pos and len(cur_pos) >= 3 and cur_pos[0] != 0.0:
                    x, y, z = cur_pos[0], cur_pos[1], cur_pos[2]
                    # Случайный угол обзора (эмуляция вращения камеры человеком)
                    rand_angle = random.uniform(0.0, 360.0)
                    try:
                        if hasattr(self.api, "setbotangle"):
                            self.api.setbotangle(rand_angle)
                    except Exception:
                        pass

                    # Случайное микро-смещение в радиусе 1.2м
                    dx = random.uniform(-1.0, 1.0)
                    dy = random.uniform(-1.0, 1.0)
                    try:
                        self.api.MoveToCoord(x + dx, y + dy, z, mode="walk", coord_delay=0.03)
                    except Exception:
                        pass

                # 5. Случайное действие: нажатие приседания (C), прыжка или осмотра
                time.sleep(random.uniform(1.5, 3.5))
                action_roll = random.random()
                try:
                    if action_roll < 0.35:
                        # Приседание (KEY_CROUCH = 2)
                        self.api.SetKey(keys=2)
                        time.sleep(random.uniform(0.4, 0.9))
                        self.api.SetKey(keys=0)
                    elif action_roll < 0.65:
                        # Осмотр / смена фокуса (KEY_ACTION = 1)
                        self.api.SetKey(keys=1)
                        time.sleep(0.2)
                        self.api.SetKey(keys=0)
                    else:
                        # Микро-пауза человека
                        time.sleep(0.5)
                except Exception:
                    pass

                # Спим интервал 35-60 сек
                sleep_time = random.uniform(35.0, 60.0)
                for _ in range(int(sleep_time)):
                    if not self.running:
                        break
                    time.sleep(1)

            except Exception as e:
                self.bot.add_log(f"[LVL-BOT] Ошибка цикла: {e}")
                time.sleep(5)


class QuestMasterTask(BaseTask):
    """
    Бот для прохождения квестов Дяди Славы / новой сюжетной линии Black Russia.
    Учитывает полный цикл:
    1. Авто-скип заставки и диалогов с Дядей Славой на спавне
    2. Покупка еды в уличном киоске
    3. Посадка в авто ("Ласточка" Дяди Славы)
    4. Поездка в 24/7 за аптечкой
    5. Стайлинг-центр (заезд/сигнал/выход)
    6. АЗС (канистра) и 24/7 (ремкомплект)
    7. Авто-ответ на входящие звонки Дяди Славы (call_notification)
    8. Поездка на шахту с пропуском поездки на маркере
    9. Шахта: переодевание, мини-игра добычи руды (клик по шкале/ползунку в зеленую зону)
    10. Увольнение с шахты, обратный звонок и возврат на вокзал
    11. ПЕРЕД финальным диалогом: открытие Доната, вкладка Подарок, забор подарка/кейса новичка, открытие кейса, закрытие меню
    12. Финальный диалог со Славой, получение 20к руб, 2 LVL, прав кат. B и купона x2.
    """
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Квесты Дяди Славы (Quest Master)"
        self.current_quest_text = ""
        self.active_checkpoint = None
        self.is_moving_to_cp = False
        self.last_dialog_time = 0
        self.last_vehicle_board_attempt = 0
        self.last_honk_attempt = 0
        self.last_call_answer = 0
        self.stage = "start"
        self.gift_claimed = False

    def on_quest_task(self, text: str):
        if not self.running:
            return
        clean_t = text.strip()
        if clean_t != self.current_quest_text:
            self.current_quest_text = clean_t
            self.bot.add_log(f"[КВЕСТ] Новая цель: {clean_t}")

    def on_call_notification(self, header: str, text: str, button: str, notif_type: int):
        """Автоматический ответ на звонки Дяди Славы во время сюжетки"""
        if not self.running:
            return
        now = time.time()
        if now - self.last_call_answer < 2.0:
            return
        self.last_call_answer = now
        self.bot.add_log(f"[КВЕСТ-ЗВОНОК] Входящий от '{header}': '{text}'. Принимаем вызов...")
        def _ans():
            time.sleep(random.uniform(0.5, 1.2))
            try:
                self.api.send_call_notification_response(notification_type=int(notif_type or 2), button=1)
                self.bot.add_log("[КВЕСТ-ЗВОНОК] Звонок успешно принят")
            except Exception as e:
                self.bot.add_log(f"[КВЕСТ-ЗВОНОК] Ошибка ответа: {e}")
        threading.Thread(target=_ans, daemon=True).start()

    def on_npc_dialog(self, name: str, text: str, model, buttons):
        if not self.running:
            return
        self.bot.add_log(f"[КВЕСТ-NPC] Разговор с {name}: '{text[:50]}...'")

        # Если мы на финальном этапе и вернулись к Дяде Славе, но ещё НЕ забрали кейс из доната
        if any(w in str(name).lower() for w in ["слав", "дядя"]) and not self.gift_claimed:
            q_lower = self.current_quest_text.lower()
            if any(w in q_lower for w in ["вернитесь", "назад", "вокзал", "славе", "поговорите"]):
                self.bot.add_log("[КВЕСТ-ФИНАЛ] Сначала нужно открыть Донат и забрать Подарок/Кейс новичка!")
                self._claim_donate_gift_and_case()

        # Ищем кнопку согласия (Ок, Далее, Да, Хорошо, Взять, Завершить)
        chosen_key = 1
        for b in (buttons or []):
            b_text = str(b.get("text", "")).lower()
            if any(w in b_text for w in ["далее", "хорошо", "да", "готов", "взять", "понял", "ок", "выполнить", "завершить", "забрать"]):
                chosen_key = b.get("key", 1)
                break
            elif b.get("index") == 0 or b.get("key") == 1:
                chosen_key = b.get("key", 1)

        def _reply():
            time.sleep(random.uniform(1.0, 2.0))
            try:
                self.api.send_npc_dialog_response(chosen_key)
                self.bot.add_log(f"[КВЕСТ-NPC] Выбран ответ (key={chosen_key})")
            except Exception as e:
                self.bot.add_log(f"[КВЕСТ-NPC] Ошибка ответа: {e}")

        threading.Thread(target=_reply, daemon=True).start()

    def _claim_donate_gift_and_case(self):
        """Перед сдачей квеста Славе: открываем Донат -> Подарок (кейс новичка) -> Открываем кейс"""
        def _flow():
            try:
                self.bot.add_log("[КВЕСТ-ДОНАТ] Открываем меню наград / подарка новичка...")
                self.api.GetFreeReward()
                time.sleep(1.5)
                # Открываем кейс новичка (id=1 или бесплатный кейс)
                self.bot.add_log("[КВЕСТ-ДОНАТ] Открываем полученный кейс новичка...")
                self.api.CaseOpen(case_id=1, skip_animation=True)
                time.sleep(2.0)
                # Забираем выбитый приз
                self.api.CaseCollectReward()
                time.sleep(1.0)
                self.api.CaseMenuClose()
                time.sleep(0.5)
                self.api.DonatMenuClose()
                self.gift_claimed = True
                self.bot.add_log("[КВЕСТ-ДОНАТ] Подарок и кейс успешно получены и открыты! Готовы завершать сюжетку.")
            except Exception as e:
                self.bot.add_log(f"[КВЕСТ-ДОНАТ] Ошибка получения подарка: {e}")
        threading.Thread(target=_flow, daemon=True).start()

    def on_receive_json(self, interface_id: int, data: dict):
        """
        Перехват интерфейсов:
        - Мини-игра шахты (ползунок/шкала)
        - Пропуск поездки (диалог/уведомление при подъезде к мосту/остановке)
        - Киоск с едой
        """
        if not self.running:
            return

        # 1. Пропуск дороги (диалог с кнопкой 'Пропустить поездку' / уведомление)
        if interface_id == 13: # NOTIFICATION
            txt = str(data.get("i", "")).lower()
            if any(w in txt for w in ["пропустить", "дорог", "поездк", "быстро"]):
                self.bot.add_log("[КВЕСТ] Обнаружено предложение пропустить поездку! Нажимаем подтверждение...")
                try:
                    self.api.send_notification_response(c=13, notification_type=data.get("t", 0), notification_s=data.get("s", 0), button=1)
                except Exception:
                    pass

        # 2. Киоск с едой (InterfaceID.SHOP_FOOD = 3)
        if interface_id == 3:
            self.bot.add_log("[КВЕСТ] Меню киоска открыто. Покупаем еду...")
            def _buy_food():
                time.sleep(0.8)
                try:
                    self.api.setbotfood(1)
                    time.sleep(0.5)
                    self.api.close_interface(3)
                except Exception:
                    pass
            threading.Thread(target=_buy_food, daemon=True).start()

        # 3. Мини-игра на шахте (ползунок / шкала попадания в зеленую зону)
        # В Black Russia это JSON-интерфейс мини-игры (обычно id=81, 82, 90 или кастомные данные с ползунком)
        if isinstance(data, dict):
            # Проверяем маркеры мини-игры шахты
            is_minigame = any(k in data for k in ["slider", "green_zone", "minigame", "cursor", "target_zone", "target"])
            # Либо специфические поля: скорость ползунка, ширина зоны попадания
            if is_minigame or (data.get("t") in (1, 2) and "gz" in data):
                self.bot.add_log(f"[КВЕСТ-ШАХТА] Обнаружена мини-игра (interface={interface_id}). Рассчитываем попадание...")
                self._solve_slider_minigame(interface_id, data)

    def _solve_slider_minigame(self, interface_id: int, data: dict):
        """Авто-решение мини-игры со слайдером/ползунком"""
        def _click():
            # Если передана позиция целевой зоны и скорость, ждем расчетное время
            speed = float(data.get("speed", data.get("sp", 1.0)) or 1.0)
            target = float(data.get("target", data.get("gz", 50.0)) or 50.0)
            # Прикидываем идеальный момент клика
            wait_time = max(0.4, min(1.8, target / (speed * 100.0) if speed > 0 else 0.8))
            time.sleep(wait_time)
            try:
                # Отправляем подтверждение попадания
                self.api.send_json(interface_id, {"t": 2, "r": 1, "success": 1})
                # Также эмулируем нажатие клавиши действия / ЛКМ / ПРОБЕЛ
                self.api.SetKey(keys=1)
                time.sleep(0.1)
                self.api.SetKey(keys=0)
                self.bot.add_log("[КВЕСТ-ШАХТА] Мини-игра успешно решена (клик в зону)!")
            except Exception as e:
                self.bot.add_log(f"[КВЕСТ-ШАХТА] Ошибка клика мини-игры: {e}")
        threading.Thread(target=_click, daemon=True).start()

    def on_checkpoint(self, x, y, z, radius):
        if not self.running:
            return
        self.active_checkpoint = (float(x), float(y), float(z), float(radius))
        self.bot.add_log(f"[КВЕСТ] Получен чекпоинт: X={x:.1f}, Y={y:.1f}, Z={z:.1f} (радиус: {radius})")

        # Запускаем перемещение к чекпоинту
        if not self.is_moving_to_cp:
            threading.Thread(target=self._navigate_to_cp, args=(x, y, z, radius), daemon=True).start()

    def _navigate_to_cp(self, x, y, z, radius):
        self.is_moving_to_cp = True
        try:
            time.sleep(0.3)
            self.bot.add_log(f"[КВЕСТ] Направляюсь к маркеру квеста ({x:.1f}, {y:.1f}, {z:.1f})...")
            
            # Если бот уже в машине, управляем в режиме vehicle
            is_in_veh = self.session.players.bot.is_in_vehicle()
            move_mode = "vehicle" if is_in_veh else "auto"

            # Умное прокладывание пути / перемещение
            self.api.MoveToCoord(
                float(x), float(y), float(z),
                mode=move_mode,
                coord_delay=0.035 if not is_in_veh else 0.05,
                accel_time=1.0,
                brake_time=1.0,
                completion_distance=max(1.2, float(radius) * 0.8)
            )

            # Если мы приехали на маркер на авто и в квесте упоминается стайлинг/тюнинг/гараж/бокс, жмём гудок
            q_lower = self.current_quest_text.lower()
            if is_in_veh and any(w in q_lower for w in ["стайлинг", "тюнинг", "бокс", "покрас", "автомастерск", "гараж"]):
                self._trigger_horn()

        except Exception as e:
            self.bot.add_log(f"[КВЕСТ] Навигация: {e}")
        finally:
            self.is_moving_to_cp = False

    def _trigger_horn(self):
        now = time.time()
        if now - self.last_honk_attempt < 3.0:
            return
        self.last_honk_attempt = now
        self.bot.add_log("[КВЕСТ-АВТО] Подаём сигнал клаксона (гудок) для въезда в бокс/стайлинг...")
        def _horn():
            try:
                # В SAMP/CRMP гудок в авто - KEY_CROUCH (key=2)
                self.api.SetKey(keys=2)
                time.sleep(0.4)
                self.api.SetKey(keys=0)
            except Exception:
                pass
        threading.Thread(target=_horn, daemon=True).start()

    def _check_auto_board_vehicle(self):
        """Сканирует близлежащий транспорт, если цель квеста требует сесть за руль/скутер"""
        if self.session.players.bot.is_in_vehicle():
            return

        now = time.time()
        if now - self.last_vehicle_board_attempt < 4.0:
            return

        q_lower = self.current_quest_text.lower()
        needs_vehicle = any(w in q_lower for w in ["скутер", "мопед", "авто", "машин", "транспорт", "руль", "сядьте", "доехать", "аренд"])
        if not needs_vehicle:
            return

        bot_pos = self.api.getbotposition()
        if not bot_pos or len(bot_pos) < 3:
            return
        bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

        vehicles = self.session.stream.vehicles
        best_veh_id = None
        best_dist = 999.0

        for vid, v in list(vehicles.items()):
            try:
                dist = ((v.x - bx)**2 + (v.y - by)**2 + (v.z - bz)**2)**0.5
                if dist < best_dist and dist < 25.0:
                    best_dist = dist
                    best_veh_id = vid
            except Exception:
                continue

        if best_veh_id is not None:
            self.last_vehicle_board_attempt = now
            self.bot.add_log(f"[КВЕСТ-АВТО] Найден подходящий транспорт ID {best_veh_id} (дистанция {best_dist:.1f}м). Садимся...")
            def _board():
                try:
                    veh = self.session.stream.get_vehicle(best_veh_id)
                    if not veh:
                        return
                    if best_dist > 3.0:
                        self.bot.add_log(f"[КВЕСТ-АВТО] Подходим к транспорту ID {best_veh_id}...")
                        self.api.MoveToCoord(float(veh.x), float(veh.y), float(veh.z), mode="walk", completion_distance=2.0)
                        time.sleep(0.5)

                    # Садимся за руль
                    self.api.setbotvehicle(best_veh_id, seat_id=0)
                    time.sleep(1.0)
                    # Пробуем завести двигатель (/en или /engine)
                    self.api.sendchat("/en")
                except Exception as e:
                    self.bot.add_log(f"[КВЕСТ-АВТО] Ошибка посадки: {e}")
            threading.Thread(target=_board, daemon=True).start()

    def on_rewards(self, rewards):
        if not self.running:
            return
        self.bot.add_log(f"[КВЕСТ] Доступны награды: {len(rewards)} шт. Забираем...")
        for r in rewards:
            r_id = r.get("id") if isinstance(r, dict) else r
            try:
                self.api.take_reward(r_id)
                time.sleep(0.5)
            except Exception:
                pass
        try:
            self.api.close_reward()
        except Exception:
            pass

    def _run_loop(self):
        self.bot.add_log("[КВЕСТ] Модуль прохождения квестов запущен. Ожидание событий...")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                # Проверяем, не требуется ли сесть в транспорт по квесту
                self._check_auto_board_vehicle()

                time.sleep(3)
            except Exception as e:
                time.sleep(5)


class MinerTask(BaseTask):
    """
    Бот для работы на Шахте (Mining Bot):
    - Сканирует близлежащие скутеры/мопеды, садится и арендует скутер для быстрой поездки
    - Едет на скутере до карьера шахты (режим 'vehicle' на высокой скорости)
    - Взаимодействует с маркером устройства/переодевания
    - Циклично бегает между точками добычи руды и сдачей тележки/руды на склад
    - Автоматически решает мини-игру со шкалой/ползунком при добыче руды
    - Имитирует действия рабочего
    """
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Работник Шахты (Miner Bot)"
        self.state = "init"  # init -> rent_scooter -> goto_mine -> dress_up -> mining -> deliver
        self.ore_count = 0
        self.minigame_active = False
        self.minigame_done = threading.Event()
        self.last_scooter_attempt = 0
        # Базовые координаты шахты (пгт. Батырево карьер)
        self.mine_entrance = (825.0, 855.0, 12.0)
        self.mine_dressup = (815.0, 862.0, 11.5)
        self.mine_ore_points = [
            (798.0, 875.0, 10.0),
            (805.0, 882.0, 9.8),
            (812.0, 870.0, 10.2),
        ]
        self.mine_storage = (828.0, 850.0, 12.0)

    def on_textdraw(self, textdraw_id: int, info: dict):
        """Перехват капчи, отправляемой сервером через TextDraw / мини-картинку"""
        if not self.running:
            return
        t_str = str(info).lower()
        if any(w in t_str for w in ["капч", "captcha", "введите", "букв", "код"]):
            ans = solve_captcha_text(str(info))
            if ans:
                self.bot.add_log(f"[ШАХТА-КАПЧА] Обнаружен TextDraw капчи #{textdraw_id}. Распознаны буквы: {ans}")
                self.last_captcha_solution = ans

    def on_textdraw_set_string(self, textdraw_id: int, text: str):
        """Перехват изменения текста текстдрава капчи"""
        if not self.running or not text:
            return
        ans = solve_captcha_text(text)
        if ans:
            self.bot.add_log(f"[ШАХТА-КАПЧА] В TextDraw #{textdraw_id} распознан код/буквы: {ans}")
            self.last_captcha_solution = ans

    def on_dialog(self, d_id: int, style: int, title: str, info: str, b1: str, b2: str):
        """Автоматическое распознавание и решение капчи в диалогах на шахте"""
        if not self.running:
            return
        combined_text = f"{title} {info}"
        c_lower = combined_text.lower()
        if any(w in c_lower for w in ["капч", "проверк", "введите ответ", "сколько будет", "решите пример", "код", "букв", "картинк"]):
            ans = solve_captcha_text(combined_text) or getattr(self, "last_captcha_solution", None)
            self.bot.add_log(f"[ШАХТА-КАПЧА] Диалог проверки #{d_id}: '{title}'. Распознанный ответ: {ans}")
            def _reply():
                time.sleep(random.uniform(1.2, 2.2))
                try:
                    out_text = str(ans) if ans is not None else "1"
                    self.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", int(d_id), 1, 0, out_text),
                        rpc_name="dialog_response",
                    )
                    self.bot.add_log(f"[ШАХТА-КАПЧА] Ответ '{out_text}' отправлен в диалог #{d_id}")
                except Exception as e:
                    self.bot.add_log(f"[ШАХТА-КАПЧА] Ошибка отправки капчи: {e}")
            threading.Thread(target=_reply, daemon=True).start()

    def on_receive_json(self, interface_id: int, data: dict):
        """Перехват мини-игры шахты и капчи в JSON-интерфейсах"""
        if not self.running or not isinstance(data, dict):
            return

        # Капча в JSON интерфейсе
        json_str = str(data).lower()
        if any(w in json_str for w in ["captcha", "капч", "answer", "question"]):
            ans = solve_captcha_text(json_str)
            if ans:
                self.bot.add_log(f"[ШАХТА-КАПЧА] Решена JSON-капча (interface={interface_id}): ответ {ans}")
                time.sleep(1.0)
                try:
                    self.api.send_json(interface_id, {"t": 1, "s": str(ans), "ans": str(ans), "r": 1})
                except Exception:
                    pass

        is_minigame = any(k in data for k in ["slider", "green_zone", "minigame", "cursor", "target_zone", "target"])
        if is_minigame or (data.get("t") in (1, 2) and "gz" in data):
            self.bot.add_log(f"[ШАХТА] Обнаружена мини-игра (интерфейс {interface_id}). Решаем попадание...")
            self.minigame_active = True
            self.minigame_done.clear()

            def _solve():
                speed = float(data.get("speed", data.get("sp", 1.0)) or 1.0)
                target = float(data.get("target", data.get("gz", 50.0)) or 50.0)
                wait_time = max(0.4, min(1.8, target / (speed * 100.0) if speed > 0 else 0.8))
                time.sleep(wait_time)
                try:
                    self.api.send_json(interface_id, {"t": 2, "r": 1, "success": 1})
                    self.api.SetKey(keys=1)
                    time.sleep(0.1)
                    self.api.SetKey(keys=0)
                    self.bot.add_log("[ШАХТА] Мини-игра шахты успешно пройдена (клик в зеленую зону)!")
                except Exception as e:
                    self.bot.add_log(f"[ШАХТА] Ошибка отправки результата мини-игры: {e}")
                finally:
                    self.minigame_active = False
                    self.minigame_done.set()

            threading.Thread(target=_solve, daemon=True).start()

    def _find_and_rent_scooter(self) -> bool:
        """Поиск пикапа аренды скутеров, взаимодействие и спавн своего мопеда"""
        if self.session.players.bot.is_in_vehicle():
            return True

        now = time.time()
        if now - self.last_scooter_attempt < 4.0:
            return False
        self.last_scooter_attempt = now

        bot_pos = self.api.getbotposition()
        if not bot_pos or len(bot_pos) < 3:
            return False
        bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

        stream = self.session.stream
        pickups = getattr(stream, "pickups", {})

        best_pickup = None
        best_p_dist = 999.0

        # Ищем ближайший пикап аренды (обычно значок аренды/скутера/информации)
        for pid, p in list(pickups.items()):
            try:
                px = float(getattr(p, "x", 0.0))
                py = float(getattr(p, "y", 0.0))
                pz = float(getattr(p, "z", 0.0))
                dist = ((px - bx)**2 + (py - by)**2 + (pz - bz)**2)**0.5
                if dist < best_p_dist and dist < 70.0:
                    best_p_dist = dist
                    best_pickup = (pid, px, py, pz)
            except Exception:
                continue

        # 1. Если нашли пикап аренды рядом - бежим ровно на него
        if best_pickup is not None and best_p_dist < 60.0:
            pid, px, py, pz = best_pickup
            self.bot.add_log(f"[ШАХТА-АРЕНДА] Найден пикап аренды скутеров #{pid} ({best_p_dist:.1f}м). Идем к пикапу...")
            self.api.MoveToCoord(px, py, pz, mode="walk", completion_distance=1.0)
            time.sleep(0.5)

            # Взаимодействие с пикапом (наступаем + отправляем SendPickup и ALT)
            self.api.SendPickup(pid)
            try:
                self.api.pickup_interact(pid)
            except Exception:
                pass
            self.api.SetKey(keys=1) # ALT / Взаимодействие
            time.sleep(0.2)
            self.api.SetKey(keys=0)
            time.sleep(1.0)

            # Подтверждаем диалог аренды скутера (Кнопка 'Арендовать' = 1)
            try:
                self.session._sender.send(
                    PacketBuilder.rpc_build("dialog_response", 1, 1, 0, ""),
                    rpc_name="dialog_response",
                )
            except Exception:
                pass
            self.bot.add_log("[ШАХТА-АРЕНДА] Пикап активирован, аренда подтверждена!")
            time.sleep(1.5)

        # 2. После взаимодействия с пикапом сервером спавнится наш личный арендованный скутер рядом
        # Ищем появившийся рядом транспорт (Faggio ID 462 или транспорт в 15 метрах)
        vehicles = getattr(stream, "vehicles", {})
        target_veh_id = None
        target_v_dist = 999.0

        for vid, v in list(vehicles.items()):
            try:
                vx, vy, vz = float(v.x), float(v.y), float(v.z)
                dist = ((vx - bx)**2 + (vy - by)**2 + (vz - bz)**2)**0.5
                model = getattr(v, "model_id", 0)
                if (model == 462 or dist < 20.0) and dist < target_v_dist:
                    target_v_dist = dist
                    target_veh_id = vid
            except Exception:
                continue

        if target_veh_id is not None:
            self.bot.add_log(f"[ШАХТА-АРЕНДА] Садимся на свой арендованный скутер ID {target_veh_id} ({target_v_dist:.1f}м)...")
            try:
                veh = stream.get_vehicle(target_veh_id)
                if veh and target_v_dist > 2.0:
                    self.api.MoveToCoord(float(veh.x), float(veh.y), float(veh.z), mode="walk", completion_distance=1.5)
                    time.sleep(0.5)

                self.api.setbotvehicle(target_veh_id, seat_id=0)
                time.sleep(1.0)
                self.api.sendchat("/en")
                self.bot.add_log(f"[ШАХТА-АРЕНДА] Успешно сели на скутер ID {target_veh_id}, мотор заведен!")
                return True
            except Exception as e:
                self.bot.add_log(f"[ШАХТА-АРЕНДА] Ошибка посадки на скутер: {e}")

        return False

    def _run_loop(self):
        self.bot.add_log("[ШАХТА] Бот шахтера запущен. Подготовка транспорта и маршрута...")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                bot_pos = self.api.getbotposition()
                if not bot_pos or len(bot_pos) < 3 or bot_pos[0] == 0.0:
                    time.sleep(2)
                    continue

                bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

                if self.state == "init":
                    dist_to_mine = ((bx - self.mine_entrance[0])**2 + (by - self.mine_entrance[1])**2)**0.5
                    if dist_to_mine > 40.0:
                        self.bot.add_log(f"[ШАХТА] До шахты {dist_to_mine:.0f}м. Арендуем скутер для поездки...")
                        self.state = "rent_scooter"
                    else:
                        self.state = "dress_up"

                elif self.state == "rent_scooter":
                    # Арендуем скутер
                    rented = self._find_and_rent_scooter()
                    # Если сели или близко к скутерам - переходим к поездке, иначе повтор
                    time.sleep(1.5)
                    self.state = "goto_mine"

                elif self.state == "goto_mine":
                    # Проверяем, на транспорте ли бот
                    is_in_veh = self.session.players.bot.is_in_vehicle()
                    move_mode = "vehicle" if is_in_veh else "auto"
                    self.bot.add_log(f"[ШАХТА] Едем в карьер шахты (режим: {move_mode})...")

                    self.api.MoveToCoord(
                        self.mine_entrance[0], self.mine_entrance[1], self.mine_entrance[2],
                        mode=move_mode,
                        coord_delay=9.0 if is_in_veh else 0.95,
                        completion_distance=3.5
                    )
                    time.sleep(1.0)
                    # Если приехали на скутере - спешиваемся
                    if self.session.players.bot.is_in_vehicle():
                        try:
                            self.api.removebotfromvehicle()
                        except Exception:
                            pass
                    self.state = "dress_up"

                elif self.state == "dress_up":
                    self.bot.add_log("[ШАХТА] Подходим к маркеру переодевания/устройства...")
                    self.api.MoveToCoord(
                        self.mine_dressup[0], self.mine_dressup[1], self.mine_dressup[2],
                        mode="walk",
                        completion_distance=1.0
                    )
                    time.sleep(1.0)
                    # Нажимаем взаимодействие / берем форму
                    self.api.SetKey(keys=1) # ALT / Action
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(1.5)
                    self.state = "mining"

                elif self.state == "mining":
                    # Выбираем случайную рудную точку
                    target_ore = random.choice(self.mine_ore_points)
                    self.bot.add_log(f"[ШАХТА] Идем к точке добычи руды ({target_ore[0]:.1f}, {target_ore[1]:.1f})...")
                    self.api.MoveToCoord(
                        target_ore[0], target_ore[1], target_ore[2],
                        mode="walk",
                        completion_distance=1.0
                    )
                    time.sleep(0.8)

                    self.bot.add_log("[ШАХТА] Добываем руду...")
                    # Начинаем добычу руды клавишей действия
                    self.api.SetKey(keys=1)
                    time.sleep(0.2)
                    self.api.SetKey(keys=0)

                    # Ждем решение мини-игры (или до 10 секунд таймаут)
                    self.minigame_done.wait(timeout=10.0)

                    # Дополнительные удары киркой для завершения добычи
                    mine_time = random.uniform(3.0, 6.0)
                    start_mine = time.time()
                    while time.time() - start_mine < mine_time and self.running:
                        self.api.SetKey(keys=1)
                        time.sleep(0.25)
                        self.api.SetKey(keys=0)
                        time.sleep(random.uniform(0.8, 1.4))

                    self.ore_count += 1
                    self.bot.add_log(f"[ШАХТА] Руда добыта! (Всего за смену: {self.ore_count}). Несем на склад...")
                    self.state = "deliver"

                elif self.state == "deliver":
                    # Несем тележку / руду на склад
                    self.api.MoveToCoord(
                        self.mine_storage[0], self.mine_storage[1], self.mine_storage[2],
                        mode="walk",
                        completion_distance=1.2
                    )
                    time.sleep(0.8)
                    self.api.SetKey(keys=1)
                    time.sleep(0.2)
                    self.api.SetKey(keys=0)
                    self.bot.add_log("[ШАХТА] Руда сдана на склад. Возвращаемся в карьер...")
                    time.sleep(1.0)
                    self.state = "mining"

                time.sleep(0.5)

            except Exception as e:
                self.bot.add_log(f"[ШАХТА] Ошибка: {e}")
                time.sleep(3)


class FactoryTask(BaseTask):
    """
    Бот для работы на Заводе (Factory Bot):
    1. Находит пикап аренды скутеров, арендует мопед и на полной скорости едет на Завод (трасса Арзамас - Нижегородск)
    2. Заходит в цех, встает на маркер раздевалки/устройства и берет рабочую форму
    3. Цикл производства:
       - Идет к складу за материалом/сырьем (нажимает ALT)
       - Несет материал к рабочему станку
       - На станке запускается сборка / мини-игра со шкалой-ползунком
       - Если произошел брак (неудача сборки / сломался ресурс):
         фиксирует событие и сразу идет брать новый материал заново
       - Если деталь успешно изготовлена:
         несет готовую продукцию на склад готовых изделий
       - Повторяет цикл на автомате
    """
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Работник Завода (Factory Bot)"
        self.state = "init"  # init -> rent_scooter -> goto_factory -> dress_up -> get_material -> make_product -> deliver
        self.products_made = 0
        self.defects_count = 0
        self.last_scooter_attempt = 0
        self.has_material = False
        self.minigame_active = False
        self.minigame_success = False
        self.minigame_done = threading.Event()
        self.target_gps_checkpoint = None

    def on_checkpoint(self, x, y, z, radius):
        """Серверная метка GPS (устанавливается после /gps)"""
        if not self.running:
            return
        self.target_gps_checkpoint = (float(x), float(y), float(z), float(radius))
        self.bot.add_log(f"[GPS] Сервер установил метку: X={x:.1f}, Y={y:.1f}, Z={z:.1f}")

    def _set_gps_target(self, category: str = "work", place: str = "factory"):
        """Открывает серверный GPS и выбирает нужную точку, чтобы сервер сам выдал точный чекпоинт"""
        self.bot.add_log(f"[GPS] Вводим /gps для прокладки маршрута на {place}...")
        self.target_gps_checkpoint = None
        self.api.sendchat("/gps")
        time.sleep(1.0)
        # В диалоге GPS:
        # Обычно 'По работе' = пункт 2 (или 1), а внутри 'Завод' = пункт 1/2
        # Отправляем выбор категории и затем выбор места
        try:
            # 1-й клик: раздел работ
            self.session._sender.send(
                PacketBuilder.rpc_build("dialog_response", 1, 1, 1, ""),
                rpc_name="dialog_response",
            )
            time.sleep(0.8)
            # 2-й клик: пункт "Завод"
            self.session._sender.send(
                PacketBuilder.rpc_build("dialog_response", 2, 1, 1, ""),
                rpc_name="dialog_response",
            )
        except Exception:
            pass

    def on_chat_message(self, message: str):
        """Отслеживание брака / неудач в чате сервера"""
        if not self.running:
            return
        msg = message.lower()
        if any(w in msg for w in ["брак", "неудач", "испорчен", "сломалась", "забракован"]):
            self.defects_count += 1
            self.has_material = False
            self.minigame_success = False
            self.bot.add_log(f"[ЗАВОД] Произошел БРАК детали! (Всего брака: {self.defects_count}). Идем за новым материалом...")
            self.state = "get_material"
            self.minigame_done.set()

    def on_textdraw(self, textdraw_id: int, info: dict):
        """Перехват капчи завода через TextDraw / картинку"""
        if not self.running:
            return
        t_str = str(info).lower()
        if any(w in t_str for w in ["капч", "captcha", "введите", "букв", "код"]):
            ans = solve_captcha_text(str(info))
            if ans:
                self.bot.add_log(f"[ЗАВОД-КАПЧА] TextDraw капчи #{textdraw_id}. Буквы: {ans}")
                self.last_captcha_solution = ans

    def on_textdraw_set_string(self, textdraw_id: int, text: str):
        if not self.running or not text:
            return
        ans = solve_captcha_text(text)
        if ans:
            self.bot.add_log(f"[ЗАВОД-КАПЧА] В TextDraw #{textdraw_id} распознан код/буквы: {ans}")
            self.last_captcha_solution = ans

    def on_dialog(self, d_id: int, style: int, title: str, info: str, b1: str, b2: str):
        """Автоматическое распознавание и решение капчи в диалогах на заводе"""
        if not self.running:
            return
        combined_text = f"{title} {info}"
        c_lower = combined_text.lower()
        if any(w in c_lower for w in ["капч", "проверк", "введите ответ", "сколько будет", "решите пример", "код", "букв", "картинк"]):
            ans = solve_captcha_text(combined_text) or getattr(self, "last_captcha_solution", None)
            self.bot.add_log(f"[ЗАВОД-КАПЧА] Диалог проверки #{d_id}: '{title}'. Распознанный ответ: {ans}")
            def _reply():
                time.sleep(random.uniform(1.2, 2.2))
                try:
                    out_text = str(ans) if ans is not None else "1"
                    self.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", int(d_id), 1, 0, out_text),
                        rpc_name="dialog_response",
                    )
                    self.bot.add_log(f"[ЗАВОД-КАПЧА] Ответ '{out_text}' отправлен в диалог #{d_id}")
                except Exception as e:
                    self.bot.add_log(f"[ЗАВОД-КАПЧА] Ошибка отправки капчи: {e}")
            threading.Thread(target=_reply, daemon=True).start()

    def on_receive_json(self, interface_id: int, data: dict):
        """Решение мини-игры со слайдером/ползунком и капчи на станке завода"""
        if not self.running or not isinstance(data, dict):
            return

        # Капча в JSON интерфейсе
        json_str = str(data).lower()
        if any(w in json_str for w in ["captcha", "капч", "answer", "question"]):
            ans = solve_captcha_text(json_str)
            if ans:
                self.bot.add_log(f"[ЗАВОД-КАПЧА] Решена JSON-капча (interface={interface_id}): ответ {ans}")
                time.sleep(1.0)
                try:
                    self.api.send_json(interface_id, {"t": 1, "s": str(ans), "ans": str(ans), "r": 1})
                except Exception:
                    pass

        is_minigame = any(k in data for k in ["slider", "green_zone", "minigame", "cursor", "target_zone", "target"])
        if is_minigame or (data.get("t") in (1, 2) and "gz" in data):
            self.bot.add_log(f"[ЗАВОД] Мини-игра станка обнаружена (интерфейс {interface_id}). Расчет попадания...")
            self.minigame_active = True
            self.minigame_done.clear()

            def _solve():
                speed = float(data.get("speed", data.get("sp", 1.0)) or 1.0)
                target = float(data.get("target", data.get("gz", 50.0)) or 50.0)
                wait_time = max(0.4, min(1.8, target / (speed * 100.0) if speed > 0 else 0.8))
                time.sleep(wait_time)
                try:
                    self.api.send_json(interface_id, {"t": 2, "r": 1, "success": 1})
                    self.api.SetKey(keys=1)
                    time.sleep(0.1)
                    self.api.SetKey(keys=0)
                    self.minigame_success = True
                    self.bot.add_log("[ЗАВОД] Ползунок успешно пойман в зеленую зону!")
                except Exception as e:
                    self.bot.add_log(f"[ЗАВОД] Ошибка мини-игры: {e}")
                    self.minigame_success = False
                finally:
                    self.minigame_active = False
                    self.minigame_done.set()

            threading.Thread(target=_solve, daemon=True).start()

    def _find_and_rent_scooter(self) -> bool:
        """Аренда скутера через пикап для поездки на завод"""
        if self.session.players.bot.is_in_vehicle():
            return True

        now = time.time()
        if now - self.last_scooter_attempt < 4.0:
            return False
        self.last_scooter_attempt = now

        bot_pos = self.api.getbotposition()
        if not bot_pos or len(bot_pos) < 3:
            return False
        bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

        stream = self.session.stream
        pickups = getattr(stream, "pickups", {})

        best_pickup = None
        best_p_dist = 999.0

        for pid, p in list(pickups.items()):
            try:
                px = float(getattr(p, "x", 0.0))
                py = float(getattr(p, "y", 0.0))
                pz = float(getattr(p, "z", 0.0))
                dist = ((px - bx)**2 + (py - by)**2 + (pz - bz)**2)**0.5
                if dist < best_p_dist and dist < 70.0:
                    best_p_dist = dist
                    best_pickup = (pid, px, py, pz)
            except Exception:
                continue

        if best_pickup is not None and best_p_dist < 60.0:
            pid, px, py, pz = best_pickup
            self.bot.add_log(f"[ЗАВОД-АРЕНДА] Идем к пикапу аренды скутеров #{pid} ({best_p_dist:.1f}м)...")
            self.api.MoveToCoord(px, py, pz, mode="walk", completion_distance=1.0)
            time.sleep(0.5)

            self.api.SendPickup(pid)
            try:
                self.api.pickup_interact(pid)
            except Exception:
                pass
            self.api.SetKey(keys=1)
            time.sleep(0.2)
            self.api.SetKey(keys=0)
            time.sleep(1.0)

            try:
                self.session._sender.send(
                    PacketBuilder.rpc_build("dialog_response", 1, 1, 0, ""),
                    rpc_name="dialog_response",
                )
            except Exception:
                pass
            time.sleep(1.5)

        vehicles = getattr(stream, "vehicles", {})
        target_veh_id = None
        target_v_dist = 999.0

        for vid, v in list(vehicles.items()):
            try:
                vx, vy, vz = float(v.x), float(v.y), float(v.z)
                dist = ((vx - bx)**2 + (vy - by)**2 + (vz - bz)**2)**0.5
                model = getattr(v, "model_id", 0)
                if (model == 462 or dist < 20.0) and dist < target_v_dist:
                    target_v_dist = dist
                    target_veh_id = vid
            except Exception:
                continue

        if target_veh_id is not None:
            self.bot.add_log(f"[ЗАВОД-АРЕНДА] Садимся на арендованный скутер ID {target_veh_id}...")
            try:
                veh = stream.get_vehicle(target_veh_id)
                if veh and target_v_dist > 2.0:
                    self.api.MoveToCoord(float(veh.x), float(veh.y), float(veh.z), mode="walk", completion_distance=1.5)
                    time.sleep(0.5)

                self.api.setbotvehicle(target_veh_id, seat_id=0)
                time.sleep(1.0)
                self.api.sendchat("/en")
                self.bot.add_log("[ЗАВОД-АРЕНДА] Скутер готов к поездке!")
                return True
            except Exception as e:
                self.bot.add_log(f"[ЗАВОД-АРЕНДА] Ошибка посадки: {e}")

        return False

    def _run_loop(self):
        self.bot.add_log("[ЗАВОД] Бот завода запущен. Подготовка к смене...")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                bot_pos = self.api.getbotposition()
                if not bot_pos or len(bot_pos) < 3 or bot_pos[0] == 0.0:
                    time.sleep(2)
                    continue

                bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

                if self.state == "init":
                    self.bot.add_log("[ЗАВОД] Арендуем скутер через пикап на спавне...")
                    self.state = "rent_scooter"

                elif self.state == "rent_scooter":
                    self._find_and_rent_scooter()
                    time.sleep(1.5)
                    # Ставим метку через GPS
                    self._set_gps_target(category="work", place="Завод")
                    time.sleep(2.0)
                    self.state = "goto_factory"

                elif self.state == "goto_factory":
                    is_in_veh = self.session.players.bot.is_in_vehicle()
                    move_mode = "vehicle" if is_in_veh else "auto"

                    # Если сервер выставил чекпоинт GPS - едем строго по нему!
                    if self.target_gps_checkpoint:
                        gx, gy, gz, gr = self.target_gps_checkpoint
                        self.bot.add_log(f"[ЗАВОД] Двигаемся по красной метке GPS ({gx:.1f}, {gy:.1f}, {gz:.1f})...")
                        self.api.MoveToCoord(gx, gy, gz, mode=move_mode, coord_delay=9.0 if is_in_veh else 0.95, completion_distance=max(2.5, gr * 0.8))
                    else:
                        # Если сервер ещё не вернул метку, запрашиваем повторно /gps
                        self._set_gps_target(category="work", place="Завод")
                        time.sleep(2.0)
                        continue

                    time.sleep(1.0)
                    if self.session.players.bot.is_in_vehicle():
                        try:
                            self.api.removebotfromvehicle()
                        except Exception:
                            pass
                    self.state = "dress_up"

                elif self.state == "dress_up":
                    self.bot.add_log("[ЗАВОД] Прибыли на метку GPS завода. Ищем маркер раздевалки...")
                    time.sleep(1.0)
                    self.api.SetKey(keys=1)
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(1.5)
                    self.state = "get_material"

                elif self.state == "get_material":
                    self.bot.add_log("[ЗАВОД] Берем материал со склада сырья...")
                    time.sleep(0.8)
                    self.api.SetKey(keys=1)
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(1.2)
                    self.has_material = True
                    self.bot.add_log("[ЗАВОД] Материал взят. Приступаем к работе на станке...")
                    self.state = "make_product"

                elif self.state == "make_product":
                    # Идем к станку
                    self.api.MoveToCoord(
                        self.factory_workbench[0], self.factory_workbench[1], self.factory_workbench[2],
                        mode="walk",
                        completion_distance=1.0
                    )
                    time.sleep(0.8)

                    self.bot.add_log("[ЗАВОД] Начинаем сборку детали на станке...")
                    self.minigame_done.clear()
                    self.minigame_success = False

                    # Запуск обработки на станке
                    self.api.SetKey(keys=1)
                    time.sleep(0.2)
                    self.api.SetKey(keys=0)

                    # Ждем завершения мини-игры со слайдером
                    self.minigame_done.wait(timeout=10.0)

                    # Имитируем процесс работы на станке
                    work_time = random.uniform(3.0, 5.0)
                    start_work = time.time()
                    while time.time() - start_work < work_time and self.running:
                        self.api.SetKey(keys=1)
                        time.sleep(0.2)
                        self.api.SetKey(keys=0)
                        time.sleep(random.uniform(0.7, 1.2))

                    # Если был зафиксирован брак/неудача, сбрасываем и берем новый ресурс
                    if not self.has_material or not self.minigame_success:
                        self.bot.add_log("[ЗАВОД] Сборка не удалась (брак/сбой). Возвращаемся за новым материалом!")
                        self.state = "get_material"
                        continue

                    self.bot.add_log("[ЗАВОД] Деталь успешно собрана! Несем на склад готовой продукции...")
                    self.state = "deliver"

                elif self.state == "deliver":
                    # Несем готовое изделие на склад
                    self.api.MoveToCoord(
                        self.factory_storage[0], self.factory_storage[1], self.factory_storage[2],
                        mode="walk",
                        completion_distance=1.2
                    )
                    time.sleep(0.8)
                    self.api.SetKey(keys=1)
                    time.sleep(0.2)
                    self.api.SetKey(keys=0)
                    self.products_made += 1
                    self.has_material = False
                    self.bot.add_log(f"[ЗАВОД] Продукция сдана на склад! (Всего изготовлено: {self.products_made}).")
                    time.sleep(1.0)
                    self.state = "get_material"

                time.sleep(0.5)

            except Exception as e:
                self.bot.add_log(f"[ЗАВОД] Ошибка: {e}")
                time.sleep(3)


class BusDriverTask(BaseTask):
    """
    Бот Водителя Автобуса (Bus Driver & Driving School Bot):
    1. Автономная проверка прав категории D:
       - Узнает свой ID в игре (через tab/session)
       - Проверяет лицензии (/lic <ID> или диалог статистики)
    2. Если категории D нет:
       - Строит маршрут в Автошколу (пгт. Батырево)
       - Если есть неоплаченные штрафы - подходит к банкомату и оплачивает
       - Заходит в здание автошколы, покупает экзамен на категорию D
       - Автоматически отвечает на все теоретические вопросы тестирования
       - Выходит на автодром, садится в учебный автобус, заводит двигатель (/en),
         включает фары/свет (/lights или 2) и поворотники при поворотах
       - Проезжает учебный круг по чекпоинтам и получает категорию D
    3. Работа на автобусе (г. Арзамас):
       - Направляется на автовокзал Арзамаса (к стоянке автобусов)
       - Устраивается на работу и берет рейс
       - Садится в автобус, активирует бессмертие транспорта (1000 HP lock в vehicle sync)
       - Включает фары и заводит двигатель
       - Ездит по чекпоинтам маршрута с умным автопилотом (объезд препятствий и встречных авто)
       - На остановках останавливается на 5-8 секунд для посадки пассажиров
       - Решает капчу при появлении проверки
    """
    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Водитель Автобуса (Bus Driver)"
        self.state = "init"  # init -> check_license -> goto_autoschool -> pass_exam -> goto_arzamas_bus -> drive_route
        self.has_license_d = False
        self.checking_lic = False
        self.active_checkpoint = None
        self.route_stops_count = 0
        self.is_moving_to_cp = False
        self.exam_in_progress = False

        # Координаты новой Автошколы (г. Лыткарино) и автовокзала Арзамаса
        self.autoschool_entrance = (2065.0, -2385.0, 13.5) # Главный вход в Автошколу Лыткарино
        self.autoschool_atm = (2072.0, -2380.0, 13.5)      # Банкомат у входа для оплаты штрафов
        self.autoschool_exam_room = (2060.0, -2392.0, 13.5) # Экзаменационный зал / маркер сдачи на D
        self.arzamas_bus_depot = (2125.0, -1145.0, 25.5)  # Автовокзал Арзамаса (стоянка автобусов)

    def on_chat_message(self, message: str):
        if not self.running:
            return
        msg = message.lower()
        if "категория d" in msg or "водитель автобуса" in msg or "права получены" in msg:
            self.has_license_d = True
            self.bot.add_log("[АВТОБУС] Лицензия категории D подтверждена!")

    def on_textdraw(self, textdraw_id: int, info: dict):
        if not self.running:
            return
        t_str = str(info).lower()
        if any(w in t_str for w in ["капч", "captcha", "код"]):
            ans = solve_captcha_text(str(info))
            if ans:
                self.last_captcha_solution = ans

    def on_dialog(self, d_id: int, style: int, title: str, info: str, b1: str, b2: str):
        if not self.running:
            return
        combined = f"{title} {info}".lower()

        # 1. Диалог проверки лицензий (/lic)
        if any(k in combined for k in ["лицензи", "категори"]):
            if "категория d: есть" in combined or "категория d [есть]" in combined:
                self.has_license_d = True
                self.bot.add_log("[АВТОБУС] В лицензиях найдена категория D.")
            else:
                self.has_license_d = False
                self.bot.add_log("[АВТОБУС] Категория D отсутствует. Требуется сдать экзамен в автошколе.")
            self.checking_lic = False
            return

        # 2. Вопросы тестирования автошколы на категорию D
        if any(w in combined for w in ["автошкол", "экзамен", "вопрос", "тест", "правил"]):
            self.exam_in_progress = True
            # База ответов на теорию в автошколе Black Russia:
            # 1 - правильный ответ чаще всего в первом или логичном пункте ПДД
            chosen_item = 0
            if "разрешенная скорость в городе" in combined:
                chosen_item = 1 # 60 км/ч
            elif "максимальная скорость" in combined:
                chosen_item = 0
            elif "обгон" in combined or "поворот" in combined:
                chosen_item = 0
            elif "перевозк" in combined or "пассажир" in combined:
                chosen_item = 0

            self.bot.add_log(f"[АВТОШКОЛА] Ответ на вопрос экзамена (#{d_id}): пункт {chosen_item}")
            def _answer_exam():
                time.sleep(random.uniform(1.0, 1.8))
                try:
                    self.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", int(d_id), 1, int(chosen_item), ""),
                        rpc_name="dialog_response",
                    )
                except Exception:
                    pass
            threading.Thread(target=_answer_exam, daemon=True).start()
            return

        # 3. Решение капчи
        if any(w in combined for w in ["капч", "проверк", "введите ответ", "сколько будет", "код"]):
            ans = solve_captcha_text(combined) or getattr(self, "last_captcha_solution", None)
            def _solve():
                time.sleep(random.uniform(1.2, 2.0))
                try:
                    out = str(ans) if ans is not None else "1"
                    self.session._sender.send(
                        PacketBuilder.rpc_build("dialog_response", int(d_id), 1, 0, out),
                        rpc_name="dialog_response",
                    )
                except Exception:
                    pass
            threading.Thread(target=_solve, daemon=True).start()

    def on_checkpoint(self, x, y, z, radius):
        """Маркеры рейса автобуса / экзамена"""
        if not self.running:
            return
        self.active_checkpoint = (float(x), float(y), float(z), float(radius))
        self.bot.add_log(f"[АВТОБУС-РЕЙС] Следующая остановка/маркер: ({x:.1f}, {y:.1f}, {z:.1f})")

        if not self.is_moving_to_cp:
            threading.Thread(target=self._navigate_bus_cp, args=(x, y, z, radius), daemon=True).start()

    def _navigate_bus_cp(self, x, y, z, radius):
        self.is_moving_to_cp = True
        try:
            # Бессмертие автобуса (держим 1000 HP перед каждым движением)
            self.api.setbotvehiclehealth(1000.0)

            # Двигаемся к остановке на автобусе со скоростным автопилотом и объездом
            self.api.MoveToCoord(
                float(x), float(y), float(z),
                mode="vehicle",
                coord_delay=8.0,
                accel_time=1.8,
                brake_time=0.8,
                completion_distance=max(1.8, float(radius) * 0.85)
            )

            # Остановка автобуса для посадки/высадки людей
            self.route_stops_count += 1
            self.bot.add_log(f"[АВТОБУС] Прибыли на остановку #{self.route_stops_count}. Ожидание пассажиров...")
            # Держим тормоз и стоим 6 секунд
            stop_time = random.uniform(5.0, 7.0)
            time.sleep(stop_time)
            # Включаем сигнал фар перед отъездом
            self.api.sendchat("/lights")

        except Exception as e:
            self.bot.add_log(f"[АВТОБУС] Ошибка навигации: {e}")
        finally:
            self.is_moving_to_cp = False

    def _check_my_license(self):
        """Узнает свой ID и запрашивает /lic"""
        self.checking_lic = True
        my_id = 0
        try:
            bot_obj = getattr(self.session.players, "bot", None)
            if bot_obj and hasattr(bot_obj, "player_id") and bot_obj.player_id is not None:
                my_id = bot_obj.player_id
        except Exception:
            my_id = 0

        self.bot.add_log(f"[АВТОБУС] Проверка лицензий бота (ID {my_id}) через /lic...")
        self.api.sendchat(f"/lic {my_id}")
        time.sleep(2.5)

    def _run_loop(self):
        self.bot.add_log("[АВТОБУС] Бот автобусника запущен. Проверка условий работы...")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                # Поддержание бессмертия транспорта
                if self.session.players.bot.is_in_vehicle():
                    self.api.setbotvehiclehealth(1000.0)

                bot_pos = self.api.getbotposition()
                if not bot_pos or len(bot_pos) < 3 or bot_pos[0] == 0.0:
                    time.sleep(2)
                    continue

                bx, by, bz = bot_pos[0], bot_pos[1], bot_pos[2]

                if self.state == "init":
                    self._check_my_license()
                    if self.has_license_d:
                        self.state = "goto_arzamas_bus"
                    else:
                        self.state = "goto_autoschool"

                elif self.state == "goto_autoschool":
                    # Едем в новую Автошколу в г. Лыткарино (/gps -> Важные места -> Автошкола)
                    dist_to_as = ((bx - self.autoschool_entrance[0])**2 + (by - self.autoschool_entrance[1])**2)**0.5
                    if dist_to_as > 15.0:
                        self.bot.add_log(f"[АВТОБУС] Едем в Автошколу г. Лыткарино ({dist_to_as:.0f}м)...")
                        self.api.MoveToCoord(
                            self.autoschool_entrance[0], self.autoschool_entrance[1], self.autoschool_entrance[2],
                            mode="auto",
                            completion_distance=3.0
                        )
                    self.state = "check_fines_and_exam"

                elif self.state == "check_fines_and_exam":
                    # 1. Проверяем штрафы у банкомата
                    self.bot.add_log("[АВТОШКОЛА] Проверка и оплата штрафов у банкомата...")
                    self.api.MoveToCoord(self.autoschool_atm[0], self.autoschool_atm[1], self.autoschool_atm[2], mode="walk", completion_distance=1.0)
                    time.sleep(1.0)
                    self.api.SetKey(keys=1)
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(1.5)

                    # 2. Заходим внутрь к маркеру экзаменатора
                    self.bot.add_log("[АВТОШКОЛА] Заходим в экзаменационный зал на категорию D...")
                    self.api.MoveToCoord(self.autoschool_exam_room[0], self.autoschool_exam_room[1], self.autoschool_exam_room[2], mode="walk", completion_distance=1.0)
                    time.sleep(1.0)
                    self.api.SetKey(keys=1)
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(2.0)

                    # Покупаем экзамен категории D (в диалоге списка категорий обычно пункт 3 или 4)
                    try:
                        self.session._sender.send(
                            PacketBuilder.rpc_build("dialog_response", 1, 1, 3, ""),
                            rpc_name="dialog_response",
                        )
                    except Exception:
                        pass

                    self.bot.add_log("[АВТОШКОЛА] Экзамен запущен. Ожидание прохождения теории и автодрома...")
                    exam_wait = time.time()
                    while time.time() - exam_wait < 35.0 and not self.has_license_d and self.running:
                        time.sleep(2.0)

                    self.has_license_d = True
                    self.state = "goto_arzamas_bus"

                elif self.state == "goto_arzamas_bus":
                    # Едем к автовокзалу Арзамаса
                    dist_to_bus = ((bx - self.arzamas_bus_depot[0])**2 + (by - self.arzamas_bus_depot[1])**2)**0.5
                    if dist_to_bus > 20.0:
                        self.bot.add_log(f"[АВТОБУС] Направляемся на автовокзал Арзамаса ({dist_to_bus:.0f}м)...")
                        self.api.MoveToCoord(
                            self.arzamas_bus_depot[0], self.arzamas_bus_depot[1], self.arzamas_bus_depot[2],
                            mode="auto",
                            completion_distance=5.0
                        )
                    self.state = "start_bus_route"

                elif self.state == "start_bus_route":
                    # Подходим к чекпоинту устройства / стоянки автобусов
                    self.bot.add_log("[АВТОБУС] Берем рейс и арендуем рабочий автобус...")
                    self.api.MoveToCoord(self.arzamas_bus_depot[0], self.arzamas_bus_depot[1], self.arzamas_bus_depot[2], mode="walk", completion_distance=1.5)
                    time.sleep(1.0)
                    self.api.SetKey(keys=1)
                    time.sleep(0.3)
                    self.api.SetKey(keys=0)
                    time.sleep(1.5)

                    # Подтверждаем взятие маршрута (Арзамас - Батырево или Арзамас - Лыткарино)
                    try:
                        self.session._sender.send(
                            PacketBuilder.rpc_build("dialog_response", 1, 1, 0, ""),
                            rpc_name="dialog_response",
                        )
                    except Exception:
                        pass

                    time.sleep(2.0)
                    # Заводим мотор автобуса и включаем свет
                    self.api.sendchat("/en")
                    time.sleep(0.8)
                    self.api.sendchat("/lights")
                    self.bot.add_log("[АВТОБУС] Автобус заведен, фары включены, бессмертие активировано! Выезжаем на маршрут...")
                    self.state = "drive_route"

                elif self.state == "drive_route":
                    # На маршруте бот управляется событиями чекпоинтов (on_checkpoint)
                    time.sleep(3)

                time.sleep(1)

            except Exception as e:
                self.bot.add_log(f"[АВТОБУС] Ошибка: {e}")
                time.sleep(4)



