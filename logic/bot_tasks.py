# Engine: Python 3 | Termux
# Module: logic/bot_tasks.py
# Specialized automation tasks: LevelGrinder & QuestMaster (Uncle Slava / Начальные квесты)

import math
import time
import threading
import random
import re

from core import logger
from logic.minigames import is_minigame_packet
from samp.packets import PacketBuilder


# Заголовки, с которых начинается настоящая капча.
_CAPTCHA_TITLES = ("проверка", "капча", "подтверждение", "античит-проверка")

# Признаки того, что это НЕ капча, а служебный экран.
_NOT_CAPTCHA = (
    "anti-cheat",
    "anti cheat",
    "кикнуты",
    "packetloss",
    "меню игрока",
    "промокод",
    "правила сервера",
)


def looks_like_captcha_dialog(title: str, info: str) -> bool:
    """Это диалог капчи, а не что-то ещё?

    Раньше проверяли подстроку «код» — и под неё попадало «Промо-КОД» из
    меню игрока, а также «Код: 25» из окна Anti-Cheat. Бот отвечал на меню
    капчей с текстом «1», то есть выбирал не тот пункт, а на окно
    античита отправлял ответ, которого сервер не ждал.
    """
    clean = re.sub(r"\{[0-9a-fA-F]{6}\}", "", f"{title} {info}").lower()
    if any(marker in clean for marker in _NOT_CAPTCHA):
        return False
    if any(word in clean for word in _CAPTCHA_TITLES):
        return True
    # «Введите код: 7123» — но не «Промокод: 7123».
    if re.search(r"(?<!про)код\b\s*[:\-]?\s*\d{3,7}", clean):
        return True
    return any(word in clean for word in ("сколько будет", "решите пример", "введите ответ"))


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


class LevelRunnerTask(BaseTask):
    """Качка уровня: из спавна — в мир, на точку и /bg.

    Раньше задача заканчивалась на вокзале: бот стоял там, где сервер
    его высадил, и в лог писалась только строка про спавн. Теперь
    выполняется полный заход движка (logic/lvl_engine.run_cycle):
    подтверждение мира по стриму игроков, минута после спавна,
    бессмертие от голода, переход на точку своего вокзала на 8 м/с и
    /bg.

    Точки живут в logic/lvl_targets.json: на каждый вокзал до трёх
    вариантов, бот берёт случайный и не повторяет предыдущий — десяток
    аккаунтов не встаёт в одну клетку.

    После /bg задача не бросает бота: держит точку и, если сервер
    откатил позицию, возвращает на неё и повторяет команду.
    """

    # Насколько позиция может уехать от точки, прежде чем возвращать.
    DRIFT_LIMIT = 30.0
    # Период проверки, что бот всё ещё на точке.
    HOLD_POLL = 15.0
    # Пауза перед повторным заходом, если мир не подтвердился.
    RETRY_DELAY = 90.0

    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Качка LVL (мир → точка → /bg)"
        self._run = None
        self._report = {"status": "idle"}

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.bot.add_log(f"[TASK] Задача '{self.name}' запущена")

    def stop(self):
        self.running = False
        if self._run is not None:
            self._run.stop()
            self._run = None
        self.bot.add_log(f"[TASK] Задача '{self.name}' остановлена")

    @property
    def report(self):
        return self._report

    # --- цикл -----------------------------------------------------------

    @property
    def _nick(self) -> str:
        return getattr(self.bot, "nickname", "") or f"#{getattr(self.bot, 'bot_id', '?')}"

    def _log(self, message: str) -> None:
        """В лог с ником: по одной строке видно, кто где встал."""
        try:
            self.bot.add_log(f"[{self._nick}] {message}")
        except Exception:
            pass

    def _cycle(self) -> dict:
        from logic.lvl_engine import LevelRun

        run = LevelRun(self.bot, log=self._log)
        self._run = run
        try:
            return run.run_once()
        finally:
            self._run = None

    def _run_loop(self):
        while self.running:
            try:
                if not getattr(self.bot.session, "connected", False):
                    time.sleep(5)
                    continue
                if not self._iterate():
                    time.sleep(self.RETRY_DELAY)
            except Exception as exc:
                self._log(f"[LVL] цикл упал: {exc!r}")
                time.sleep(10)

    def _iterate(self) -> bool:
        """Один заход. True — дошли до точки и держим её."""
        report = self._cycle()
        self._report = report
        status = report.get("status")

        if status == "done":
            station = report.get("station")
            point = report.get("target") or []
            coords = ", ".join(f"{v:.1f}" for v in point) if point else "?"
            idx = report.get("index")
            number = idx + 1 if idx is not None else "?"
            self._log(
                f"[LVL] НА ТОЧКЕ: {station} | точка {number} ({coords}) | "
                f"рядом было {report.get('players', 0)} игрок(ов) | "
                f"/bg {'отправлен' if report.get('command_sent') else 'не ушёл'}"
            )
            self._hold(report)
            return True

        if status in ("no_targets", "world_unconfirmed", "short", "warp_failed"):
            self._log(
                f"[LVL] заход не завершён ({status}) — повтор через "
                f"{self.RETRY_DELAY:.0f}с"
            )
        return False

    def _hold(self, report: dict) -> None:
        """Держать точку: если сервер откатил — вернуть и повторить /bg."""
        from logic.lvl_engine import travel_to_point

        point = report.get("target")
        if not point:
            return
        point = tuple(float(v) for v in point)
        station = report.get("station")
        while self.running:
            if not getattr(self.bot.session, "connected", False):
                time.sleep(5)
                continue
            time.sleep(self.HOLD_POLL)
            if not self.running:
                return
            try:
                pos = self.bot.api.getbotposition() if self.bot.api else None
            except Exception:
                pos = None
            if not pos:
                continue
            drift = math.dist((float(pos[0]), float(pos[1]), float(pos[2])), point)
            if drift <= self.DRIFT_LIMIT:
                continue
            self._log(
                f"[LVL] снесло с точки на {drift:.0f}м ({station}) — возвращаюсь"
            )
            try:
                travel_to_point(self.bot, point, log=self._log)
                self.bot.api.sendchat("/bg")
                self._log(f"[LVL] снова на точке, /bg повторён")
            except Exception as exc:
                self._log(f"[LVL] вернуться не вышло: {exc!r}")


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
        # Живое поведение: прогулки, осмотр, простой, перерывы.
        # Раньше здесь был жёсткий цикл «шаг на метр раз в N секунд».
        from logic.humanizer import Humanizer

        self._humanizer = Humanizer(
            bot_instance,
            bot_instance.api,
            bot_instance.session,
            log=lambda m: bot_instance.add_log(m),
        )


    def on_chat_message(self, message: str):
        """Интеллектуальный авто-ответ на вопросы игроков и админов рядом, в /pm и общем чате"""
        if not self.running:
            return
        clean_msg = re.sub(r'\{[0-9a-fA-F]{6}\}', '', message).strip()
        msg_l = clean_msg.lower()

        # 1. Проверка на сообщение в /pm от администратора
        # Например: "[PM] Администратор Nick_Name: Вы тут?", "Администратор ... вам в /pm: вы тут?"
        is_pm = any(k in msg_l for k in ["/pm", "[pm]", "пм", "шепчет вам", "вам в пм", "администратор"])

        # 2. Вопросы проверки на бота
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

        if is_check or is_pm:
            # Выбираем естественный человеческий ответ
            if is_pm:
                answer = random.choice(["Да, я тут", "тут я, не афк", "да, играю", "тут", "да, на месте"])
            elif "ку" in msg_l or "привет" in msg_l:
                answer = random.choice(["ку", "ку бро", "привет", "куку", "дарова"])
            elif "бот" in msg_l:
                answer = random.choice(["сам ты бот ахах", "не, я тут", "нет, играю", "я тут, че надо?"])
            else:
                answer = random.choice(["тут", "да тут я", "да", "тут я", "тут, че хотел?", "+", "да, на месте"])

            self.bot.add_log(f"[LVL-ЧАТ] Замечено обращение/проверка: '{clean_msg}'. Отвечаем: '{answer}'")
            def _answer():
                # Случайная задержка печати (от 2 до 4.5 секунд)
                time.sleep(random.uniform(2.1, 4.5))
                try:
                    if is_pm and "/" in clean_msg:
                        # Если админ просил ответить в /b или /pm
                        self.api.sendchat(f"/b {answer}")
                    else:
                        self.api.sendchat(answer)
                    self.bot.add_log(f"[LVL-ЧАТ] Ответ отправлен: '{answer}'")
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

    def _eat_if_hungry(self):
        """Подлечиться, если просело здоровье. Живой игрок ест, а не игнорит."""
        try:
            if random.random() < 0.5:
                self.api.sendchat("/eat")
            else:
                self.api.sendchat("/food")
            self.bot.add_log("[LVL-ЕДА] Здоровье просело — поел.")
        except Exception:
            pass

    def _ensure_spawn_in_real_world(self):
        """Довести бота до общего мира — так же, как это делается вручную.

        Раньше здесь ждали меню вокзалов и на этом заканчивали. Проблема
        не в меню: после успешной авторизации сервер разрешает спавн, но
        если клиент не отправил ответный Spawn, позиция есть, статус
        «заспавнен», а стрим пустой. Бот стоит в мире один и не видит
        ни игроков, ни машин.

        Теперь порядок полный: дождаться позиции, один раз завершить
        спавн, дождаться других игроков в стриме, а если их нет —
        перейти на вокзал и проверить снова. Возвращает отчёт, поэтому
        в логе видно, чем всё кончилось, а не «вроде зашли».
        """
        try:
            from logic.real_world import ensure as enter_real_world
        except Exception as exc:
            self.bot.add_log(f"[МИР] модуль входа недоступен: {exc}")
            return None

        result = enter_real_world(self.bot, log=self.bot.add_log)
        status = result.get("status")
        if status == "confirmed":
            names = ", ".join(p["name"] for p in result.get("players", [])[:5])
            self.bot.add_log(
                f"[МИР] подтверждён за {result.get('seconds', '?')}с, рядом: {names}"
            )
        else:
            self.bot.add_log(
                f"[МИР] не подтверждён ({status}) — продолжаю в том же мире"
            )
        return result

    def _sleep_in_game(self, seconds: float):
        """Простой, не выходя из игры.

        Раньше перерыв означал разрыв сессии и повторный вход через
        полчаса. Каждый такой вход — новый хэндшейк, и сервер на них
        реагирует банами за частые входы. Здесь бот просто перестаёт
        дёргаться и остаётся в сети.

        Спим кусками по секунде: иначе остановка бота ждала бы конца
        всего простоя.
        """
        deadline = time.time() + max(0.0, seconds)
        while self.running and time.time() < deadline:
            time.sleep(1.0)

    def _run_loop(self):
        self.bot.add_log("[LVL-BOT] Режим качки уровня активирован. Поддержание активности 24/7.")
        pause_start_time = None
        # Вход в общий мир проверяем только когда бот реально в игре.
        # Раньше проверка стояла прямо перед циклом и на аккаунте в
        # очереди ждала бы впустую: позиции нет, а цикл уже должен
        # крутиться. Здесь она повторяется, пока не подтвердится.
        world_checked = False
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(5)
                    continue

                if not world_checked and "В игре" in self.bot.status:
                    world_checked = True
                    self._ensure_spawn_in_real_world()

                # 1. Здоровье.
                #
                # Раньше на нулевом опыте включался godmode_zero_xp, и в
                # каждом sync-пакете уходило ровно 100 HP — вечно, при
                # любых обстоятельствах. Живой игрок так не выглядит:
                # он получает урон, лечится, у него падает здоровье от
                # голода. Постоянные 100.0 — это подпись.
                #
                # Теперь здоровье живёт своей жизнью: бот «ест», когда
                # просело, и восстанавливается постепенно, а не мгновенно
                # щёлкает на максимум.
                bot_player = getattr(self.session.players, "bot", None)
                cur_lvl = self.bot.get_numeric_level() if hasattr(self.bot, "get_numeric_level") else 1
                is_zero_xp = (cur_lvl <= 1 and ("0/" in self.bot.level_str or self.bot.level_str == "-" or "1 lvl" in self.bot.level_str))
                if bot_player:
                    # Флаг оставляем только как страховку от смерти на нуле,
                    # но применяем его не всегда, а когда здоровье реально
                    # упало ниже опасной черты.
                    if is_zero_xp:
                        try:
                            hp = float(getattr(bot_player, "health", 100.0) or 0.0)
                        except (TypeError, ValueError):
                            hp = 100.0
                        bot_player.godmode_zero_xp = hp < 25.0
                        if hp < 45.0:
                            self._eat_if_hungry()
                    else:
                        bot_player.godmode_zero_xp = False

                # 2. Детект игроков рядом (эмуляция короткого AFK)
                # ВАЖНО: Никогда полностью не глушим пакеты синхронизации (is_paused), чтобы сервер не считал соединение разорванным
                nearby_player_found = False
                cur_pos = self.api.getbotposition()
                if cur_pos and len(cur_pos) >= 3 and cur_pos[0] != 0.0:
                    bx, by, bz = cur_pos[0], cur_pos[1], cur_pos[2]
                    streamed = self.session.players.get_streamed_players()
                    for pid, p in streamed.items():
                        if getattr(p, "is_npc", False) in (True, 1):
                            continue
                        px, py, pz = getattr(p, "x", 0.0), getattr(p, "y", 0.0), getattr(p, "z", 0.0)
                        dist = ((bx - px)**2 + (by - py)**2 + (bz - pz)**2) ** 0.5
                        if 0.5 < dist < 9.0: # Игрок рядом
                            nearby_player_found = True
                            break

                now = time.time()
                if nearby_player_found:
                    if pause_start_time is None:
                        pause_start_time = now
                    # Если игрок рядом, просто не бегаем активно, стоим на месте (но шлем onfoot-sync!)
                    if (now - pause_start_time) < 25.0:
                        time.sleep(random.uniform(3.0, 5.0))
                        continue
                    else:
                        pause_start_time = None
                else:
                    pause_start_time = None

                # 3. Забираем бесплатные награды из донат-меню раз в 30 минут
                # И забираем ежедневный календарь / календарные подарки раз в час
                # Раньше проверка шла ровно раз в 1800 секунд — по минуте
                # в минуту, как по расписанию. Живой игрок заходит в меню
                # когда вспомнит, а не по таймеру.
                reward_interval = getattr(self, "_reward_interval", None)
                if reward_interval is None:
                    reward_interval = random.uniform(1500.0, 3300.0)
                    self._reward_interval = reward_interval
                if now - self.last_action > reward_interval:
                    try:
                        self.api.GetFreeReward()
                        self.api.DonatMenuClose()
                    except Exception:
                        pass
                    # Авто-ввод промокодов/бонусов и проверка /reward
                    try:
                        self.api.sendchat("/reward")
                    except Exception:
                        pass
                    self.last_action = now
                    self._reward_interval = random.uniform(1500.0, 3300.0)

                # 4. Проверка и автоматическое повышение уровня через /mm при накоплении EXP
                if hasattr(self.bot, "stats_pending") and not self.bot.stats_pending:
                    if hasattr(self.bot, "get_numeric_level"):
                        cur_lvl = self.bot.get_numeric_level()
                        tgt_lvl = getattr(self.bot, "target_lvl", None)
                        if tgt_lvl and cur_lvl >= tgt_lvl:
                            self.bot.add_log(f"[LVL-ЦЕЛЬ] 🎉 Достигнут целевой уровень {cur_lvl}/{tgt_lvl}!")

                # 4. Живое поведение вместо прежнего «шаг на полтора метра
                # раз в девять минут».
                #
                # Старый блок делал ровно одно и то же: ждал случайные
                # 20-540 секунд, шагал 1-2 метра в случайную сторону и
                # пытался повернуть камеру методом setbotangle, которого
                # не существует — вызов молча падал, и бот не поворачивался
                # вообще никогда. Со стороны это выглядело как фигура,
                # которая стоит неподвижно, изредка дёргается на метр и
                # смотрит вечно в одну сторону. За такое и банят.
                sleep_time = self._humanizer.step()
                if sleep_time < 0:
                    # Перерывы с выходом из игры отключены. Каждый выход —
                    # это новый вход, а сервер банит за частые входы: часть
                    # аккаунтов уходила в бан и больше не заходила. Аккаунт
                    # остаётся в сети, просто замирает на месте.
                    pause = abs(sleep_time)
                    self.bot.add_log(
                        f"[LVL-ОТДЫХ] Тихий простой {pause / 60:.0f} мин без выхода из игры."
                    )
                    self._humanizer.new_session()
                    self._sleep_in_game(pause)
                    continue

                self.bot.add_log(
                    f"[LVL-ЖИВОЙ] {self._humanizer.last_activity}. "
                    f"Пауза {sleep_time:.1f} сек."
                )
                start_sleep = time.time()
                while self.running and (time.time() - start_sleep < sleep_time):
                    time.sleep(1.0)

            except Exception as e:
                self.bot.add_log(f"[LVL-BOT] Ошибка цикла: {e}")
                time.sleep(5)


class QuestMasterTask(BaseTask):
    """Квесты Дяди Славы — тонкая обёртка над logic/slava.py.

    Раньше здесь была своя реализация: маркеры, посадка, гудок, донат.
    Она работала наполовину — мини-игра искалась по ключам, которых в
    протоколе нет, а навигация не различала пешком и на машине.

    Теперь всё это в движке `logic.slava.SlavaQuest`: стадии, маршрут,
    машина, диалоги, звонки, мини-игра, подарок. Задача только
    прокидывает в него события — так `bot.py` не надо менять.
    """

    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = "Квесты Дяди Славы (Quest Master)"

        from logic.slava import SlavaQuest

        self.engine = SlavaQuest(bot_instance)
        bot_instance.slava_quest = self.engine

        # Совместимость со старым кодом: часть мест читает эти поля.
        self.current_quest_text = ""
        self.active_checkpoint = None
        self.is_moving_to_cp = False
        self.gift_claimed = False
        self.stage = "start"

    # ---- управление --------------------------------------------------

    def start(self):
        if self.running:
            return
        self.running = True
        self.engine.start()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.bot.add_log(f"[TASK] Задача '{self.name}' запущена")

    def stop(self):
        self.running = False
        try:
            self.engine.stop()
        except Exception:
            pass
        self.bot.add_log(f"[TASK] Задача '{self.name}' остановлена")

    # ---- события от сервера ------------------------------------------

    def on_quest_task(self, text: str):
        if not self.running:
            return
        self.current_quest_text = str(text or "")
        self.engine.on_quest_text(text)
        if self.engine.stage is not None:
            self.stage = self.engine.stage.key

    def on_checkpoint(self, x, y, z, radius):
        if not self.running:
            return
        self.active_checkpoint = (x, y, z, radius)
        self.engine.on_checkpoint(x, y, z, radius)
        self.bot.add_log(
            f"[КВЕСТ] маркер X={float(x):.1f} Y={float(y):.1f} "
            f"Z={float(z):.1f} (r={float(radius or 0):.1f})"
        )

    def on_npc_dialog(self, name: str, text: str, model, buttons):
        if not self.running:
            return
        self.bot.add_log(f"[КВЕСТ-NPC] {name}: '{str(text)[:60]}'")
        self.engine.on_npc_dialog(name, text, model, buttons)

    def on_call_notification(self, header: str, text: str, button: str,
                             notif_type: int):
        if not self.running:
            return
        self.engine.on_call(header, text, button, notif_type)

    def on_receive_json(self, interface_id: int, data: dict):
        if not self.running:
            return
        self.engine.on_json(interface_id, data)

    def on_rewards(self, rewards):
        if not self.running:
            return
        self.bot.add_log(f"[КВЕСТ] наград: {len(rewards)}, забираем")
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

    def on_dialog(self, d_id: int, style: int, title: str, info: str,
                  b1: str = "", b2: str = ""):
        """Сюжетные диалоги-меню (не реплики НПС)."""
        if not self.running:
            return
        from logic.slava import match_stage

        text = f"{title} {info}"
        if match_stage(text) is not None:
            self.engine.on_quest_text(text)

    # ---- цикл --------------------------------------------------------

    def _run_loop(self):
        self.bot.add_log("[КВЕСТ] движок прохождения запущен")
        while self.running:
            try:
                if not self.session.connected:
                    time.sleep(3)
                    continue
                # Вся работа — в потоке движка. Здесь только держим задачу
                # живой и обновляем совместимые поля.
                self.gift_claimed = self.engine.gift_claimed
                if self.engine.stage is not None:
                    self.stage = self.engine.stage.key
                time.sleep(2)
            except Exception as e:
                self.bot.add_log(f"[КВЕСТ] сбой цикла: {e}")
                time.sleep(3)

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
        # Последний пришедший диалог: аренда скутера подтверждается им,
        # а не выдуманным dialog_id=1 (см. _find_and_rent_scooter).
        self.last_dialog_id = None
        # Точные координаты шахты и спавнов Нижегородской области
        self.mine_territory = (2378.989, 1670.212, 13.782)
        self.mine_entrance = (2379.218, 1677.226, 15.284)
        self.mine_dressup = (540.554, 515.490, 1000.315)
        self.mine_ore_points = [
            (542.027, 535.701, 998.669),
        ]
        self.mine_storage = (557.287, 516.363, 999.710)
        self.afk_spot = (1549.989, 2717.540, 13.880)

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
        # Запоминаем ЛЮБОЙ диалог, а не только капчу: аренда скутера
        # подтверждается ответом в этот же d_id.
        try:
            self.last_dialog_id = int(d_id)
        except Exception:
            pass
        if not looks_like_captcha_dialog(title, info):
            return
        combined_text = f"{title} {info}"
        ans = solve_captcha_text(combined_text) or getattr(self, "last_captcha_solution", None)
        self.bot.add_log(f"[ШАХТА-КАПЧА] Диалог проверки #{d_id}: '{title}'. Распознанный ответ: {ans}")
        def _reply():
            time.sleep(random.uniform(1.2, 2.2))
            out_text = str(ans) if ans is not None else "1"
            if self.bot.menu_answerer.answer(
                self.session, int(d_id), list_item=0, response=1,
                text=out_text, note=f"шахта-капча → '{out_text}'",
            ):
                self.bot.add_log(f"[ШАХТА-КАПЧА] Ответ '{out_text}' отправлен в диалог #{d_id}")
        threading.Thread(target=_reply, daemon=True).start()

    def on_receive_json(self, interface_id: int, data: dict):
        """Перехват мини-игры шахты.

        Капчу здесь НЕ трогаем: bot.py вызывает logic.captcha.handle_captcha
        раньше задачи, и она уже отправила корректный {"t":0,"s":"..."} в iid 26.
        Раньше тут стоял второй ответ по выдуманным ключам
        {"t":1,"s":...,"ans":...,"r":1} — он летел следом и только мешал.
        """
        if not self.running or not isinstance(data, dict):
            return

        # Мини-игра — интерфейс 31, ключ "g". Ответ {"s":1} уже отправил
        # logic.minigames.handle_minigame (bot.py вызывает его до задачи).
        # Здесь только отмечаем факт, чтобы _run_loop не добивал клавишей.
        if is_minigame_packet(interface_id, data):
            self.bot.add_log(f"[ШАХТА] Мини-игра, режим g={data.get('g')} — отвечено")
            self.minigame_active = True
            self.minigame_done.set()

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

            # Подтверждаем диалог аренды скутера. Раньше здесь был
            # rpc_build("dialog_response", 1, 1, 0, "") — dialog_id прибит
            # гвоздями к 1, поэтому ответ уходил в пустоту и аренда не
            # подтверждалась. Теперь отвечаем в тот диалог, что реально пришёл.
            d_id = self.last_dialog_id
            if d_id is not None:
                if self.bot.menu_answerer.answer(
                    self.session, int(d_id), list_item=0, response=1,
                    note="шахта: аренда скутера",
                ):
                    self.bot.add_log(f"[ШАХТА-АРЕНДА] Аренда подтверждена в диалоге #{d_id}")
            else:
                self.bot.add_log("[ШАХТА-АРЕНДА] Диалог аренды не приходил — подтверждать нечего")
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
                    # Реальная точка добычи у нас одна, поэтому "random.choice"
                    # по списку из одного элемента ничего не рандомизировал —
                    # бот вставал в ту же точку с точностью до сантиметра.
                    # Разбрасываем подход в радиусе метра.
                    ox, oy, oz = random.choice(self.mine_ore_points)
                    jitter = 1.2
                    tx = ox + random.uniform(-jitter, jitter)
                    ty = oy + random.uniform(-jitter, jitter)
                    self.bot.add_log(f"[ШАХТА] Идем к точке добычи руды ({tx:.1f}, {ty:.1f})...")
                    self.api.MoveToCoord(tx, ty, oz, mode="walk", completion_distance=1.0)
                    time.sleep(0.8)

                    self.bot.add_log("[ШАХТА] Добываем руду...")
                    # Сбрасываем флаг ДО удара: иначе wait() ниже возвращался
                    # мгновенно на событии от прошлого цикла (clear+set подряд
                    # в on_receive_json делали ожидание мёртвым).
                    self.minigame_done.clear()
                    self.minigame_active = False
                    self.api.SetKey(keys=1)
                    time.sleep(0.2)
                    self.api.SetKey(keys=0)

                    # Ждём мини-игру. Ответ {"s":1} отправляет logic.minigames
                    # раньше задачи — здесь только ждём сам факт прихода.
                    got_minigame = self.minigame_done.wait(timeout=10.0)

                    if got_minigame:
                        # Мини-игра решена — не долбим клавишей поверх неё.
                        time.sleep(random.uniform(0.4, 1.0))
                    else:
                        # Мини-игры не было — добиваем клавишей, как раньше.
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
        if not looks_like_captcha_dialog(title, info):
            return
        combined_text = f"{title} {info}"
        ans = solve_captcha_text(combined_text) or getattr(self, "last_captcha_solution", None)
        self.bot.add_log(f"[ЗАВОД-КАПЧА] Диалог проверки #{d_id}: '{title}'. Распознанный ответ: {ans}")
        def _reply():
            time.sleep(random.uniform(1.2, 2.2))
            out_text = str(ans) if ans is not None else "1"
            if self.bot.menu_answerer.answer(
                self.session, int(d_id), list_item=0, response=1,
                text=out_text, note=f"завод-капча → '{out_text}'",
            ):
                self.bot.add_log(f"[ЗАВОД-КАПЧА] Ответ '{out_text}' отправлен в диалог #{d_id}")
        threading.Thread(target=_reply, daemon=True).start()

    def on_receive_json(self, interface_id: int, data: dict):
        """Решение мини-игры со слайдером/ползунком на станке завода.

        Капчу здесь НЕ трогаем: bot.py вызывает logic.captcha.handle_captcha
        раньше задачи, и она уже отправила корректный {"t":0,"s":"..."} в iid 26.
        Раньше следом летел второй ответ по выдуманным ключам
        {"t":1,"s":...,"ans":...,"r":1} и только мешал.
        """
        if not self.running or not isinstance(data, dict):
            return

        # Мини-игра станка — тот же интерфейс 31, ключ "g". Ответ уже
        # отправлен logic.minigames.handle_minigame. Здесь только флаги для
        # цикла задачи; выдуманный {"t":2,"r":1,"success":1} убран.
        if is_minigame_packet(interface_id, data):
            self.bot.add_log(
                f"[ЗАВОД] Мини-игра станка, режим g={data.get('g')} — отвечено"
            )
            self.minigame_active = True
            self.minigame_success = True
            self.minigame_done.set()

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
                    got_minigame = self.minigame_done.wait(timeout=10.0)

                    if got_minigame:
                        # Мини-игра решена — не долбим клавишей поверх неё.
                        time.sleep(random.uniform(0.4, 1.0))
                    else:
                        # Мини-игры не было — имитируем работу на станке.
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



