"""Сборщик урожая на ферме Black Russia.

Полный цикл:

    спавн
      -> точка фермы (1652.665, 1301.039, 12.119)
      -> НПС (1641.672, 1297.149, 12.140)
         меню "Основное меню"  -> пункт "Начать / Закончить работу"
         меню "Выбор работы"   -> пункт "Сборщик урожая"
         окно "ПРОВЕРКА"       -> капча, решается через logic.captcha
      -> точка работы (1188.963, 1267.451, 19.153)
         мини-игра "Сборщик фруктов": тапать яблоки, набрать 5.0 кг
      -> точка сдачи (1189.129, 1313.498, 18.644)
      -> снова к точке работы

ВАЖНО про транспорт. Меню фермы приходят JSON-интерфейсом iid=10
(USER_INTERFACE_SYNC), а НЕ SA-MP диалогом через RPC 347. Это два разных
механизма, и раньше модуль слушал не тот: диалогов не было никогда, а
ответ уходил RPC 62, который сервер для меню игнорирует.

Пункты меню выбираются по ТЕКСТУ, а не по номеру. Номер — догадка, и она
уже подводила: пункт "Начать / Закончить работу" меняет смысл на каждом
цикле, а порядок пунктов сервер может переставить.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque

from . import captcha as captcha_mod

FARM_LOCATION = (1652.665, 1301.039, 12.119)
FARM_NPC = (1641.672, 1297.149, 12.140)
FARM_WORK = (1188.963, 1267.451, 19.153)
FARM_MINIGAME = (1202.184, 1263.875, 20.607)
FARM_DROPOFF = (1189.129, 1313.498, 18.644)

# Тексты пунктов. Ищем по подстроке, регистр не важен.
MENU_START_WORK = "Начать / Закончить"
MENU_START_WORK_ALT = "Начать работу"
MENU_JOB_HARVESTER = "Сборщик урожая"
MENU_JOB_HARVESTER_ALT = "Сборщик фруктов"
CAPTCHA_TITLE = "ПРОВЕРКА"
MINIGAME_TITLE = "Сборщик фруктов"

# Старые имена оставлены: на них ссылаются тесты и агент.
MENU_MAIN_TITLE = MENU_START_WORK
MENU_JOB_TITLE = MENU_JOB_HARVESTER
ITEM_START_WORK = 3
ITEM_JOB_HARVESTER = 1

MINIGAME_TARGET_KG = 5.0

# Сколько ждать меню после подбора пикапа и сколько всего попыток подбора.
MENU_WAIT = 6.0
MENU_POKES = 8
# Окно ожидания капчи: сервер присылает её на 100-300мс позже ответа на
# "Выбор работы". Без окна бот уходил на работу, не пройдя проверку.
CAPTCHA_WAIT = 10.0
# Дедлайн на весь блок меню. После него — не смерть раннера, а перезаход.
MENU_DEADLINE = 120.0

# Состояния
S_IDLE = "idle"
S_TO_FARM = "to_farm"
S_TO_NPC = "to_npc"
S_WAIT_MENU = "wait_menu"
S_PICK_START = "pick_start"
S_PICK_JOB = "pick_job"
S_CAPTCHA = "captcha"
S_TO_WORK = "to_work"
S_MINIGAME = "minigame"
S_TO_DROPOFF = "to_dropoff"
S_RETRY = "retry"
S_DONE = "done"
S_FAILED = "failed"


COLOR_TAG = re.compile(r"\{[0-9A-Fa-f]{6}\}|\{[0-9A-Fa-f]{8}\}")
STYLE_TABLIST_HEADERS = 5
STYLE_TABLIST = 4
STYLE_LIST = 2


def strip_colors(text) -> str:
    """Убрать SA-MP цветовые теги: {E0584B}, {FFFFFF} и подобные."""
    return COLOR_TAG.sub("", str(text or "")).strip()


def parse_list_dialog(info, style=STYLE_TABLIST_HEADERS) -> list[str]:
    """Строки списка из диалога style 2/4/5.

    Для TABLIST_HEADERS первая строка — заголовок колонок, и list_item её
    НЕ считает. Именно поэтому жёсткий индекс 2 попадал не туда: он не
    учитывал, что строк на одну меньше, чем строк текста.
    """
    rows = [line for line in str(info or "").split("\n") if line.strip()]
    if style == STYLE_TABLIST_HEADERS and rows:
        rows = rows[1:]
    return [strip_colors(row) for row in rows]


def item_text(item) -> str:
    """Текст пункта меню из строки или объекта."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("t", "n", "name", "text", "title", "v"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return str(item)
    return str(item)


def find_item(items, *needles) -> int | None:
    """Индекс пункта, чей текст содержит любую из подстрок.

    Сравнение регистронезависимое: сервер шлёт то «Начать / Закончить
    работу», то «НАЧАТЬ РАБОТУ».
    """
    lowered = [needle.lower() for needle in needles if needle]
    for index, item in enumerate(items):
        text = item_text(item).lower()
        for needle in lowered:
            if needle in text:
                return index
    return None


def find_item_id(items, *needles):
    """Значение идентификатора пункта, если сервер кладёт его в объект.

    В меню вокзалов пункты несут id, и отвечать надо именно им, а не
    индексом. Для строк id нет — вернётся None.
    """
    index = find_item(items, *needles)
    if index is None:
        return None
    item = items[index]
    if isinstance(item, dict):
        for key in ("id", "i", "s", "v"):
            value = item.get(key)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                return value
    return None


class FarmRunner:
    """Ведёт бота по циклу фермы, по одному переходу за step()."""

    def __init__(
        self,
        bot,
        *,
        travel,
        log=None,
        captcha_chain=None,
        on_minigame=None,
        pickup=None,
        answer=None,
        arrive_radius=3.0,
    ):
        self.bot = bot
        self.travel = travel
        self.pickup = pickup or _default_pickup
        self.answer = answer or self._send_menu_answer
        self.log = log or (lambda msg: None)
        self.captcha_chain = captcha_chain or captcha_mod.SolverChain(
            [captcha_mod.PacketTextSolver()]
        )
        self.on_minigame = on_minigame
        self.arrive_radius = float(arrive_radius)

        self.state = S_IDLE
        self.started_at = None
        self.cycles = 0

        # Очередь, а не один слот: два меню могут прийти в одном тике, и
        # раньше второе затирало первое.
        self.menus: deque = deque(maxlen=8)
        self.dialogs: deque = deque(maxlen=8)
        self.dialog = None
        self.dialog_at = 0.0
        self.challenge = None
        self.minigame_active = False
        self.minigame_started_at = 0.0
        self.kg = 0.0
        self._poked = False
        self._pokes = 0
        self._menu_deadline = 0.0
        self._pick_job_at = 0.0
        self._start_sent = False
        self._raw_menu = None

    # ---- события -----------------------------------------------------

    def on_dialog_menu(self, items, closed=False, payload=None):
        """JSON-меню iid=10. Именно так приходят меню фермы."""
        if closed:
            self.menus.clear()
            self.log("[farm] меню закрыто сервером")
            return
        if not items:
            return
        entry = {
            "items": list(items),
            "payload": payload or {},
            "at": time.time(),
        }
        self.menus.append(entry)
        self._raw_menu = payload
        self.log(f"[farm] меню iid=10: {[item_text(i) for i in items]}")

    def on_dialog(self, dialog_id, style, title, info, button1="", button2=""):
        """SA-MP диалог. Меню фермы и капча приходят именно так.

        style 5 — TABLIST_HEADERS (меню с колонками), style 4 — TABLIST,
        style 2 — обычный список. Строки берём через parse_list_dialog,
        чтобы первая строка-заголовок не сдвигала индексы.
        """
        entry = {
            "dialog_id": dialog_id,
            "style": style,
            "title": strip_colors(title),
            "raw_title": title or "",
            "info": info or "",
            "items": parse_list_dialog(info, style),
            "button1": button1 or "",
            "button2": button2 or "",
            "at": time.time(),
        }
        self.dialog = entry
        self.dialog_at = time.time()
        self.dialogs.append(entry)
        self.log(
            f"[farm] диалог id={dialog_id} style={style} title={entry['title']!r} "
            f"пунктов={len(entry['items'])}"
        )

    def on_minigame_packet(self, kg=None, active=None):
        if kg is not None:
            self.kg = float(kg)
        if active is not None:
            self.minigame_active = bool(active)
            if self.minigame_active and not self.minigame_started_at:
                self.minigame_started_at = time.time()

    # ---- служебное ---------------------------------------------------

    def _pos(self):
        return self.bot.api.getbotposition()

    def _dist(self, target):
        try:
            return math.dist(self._pos(), target)
        except Exception:
            return 99999.0

    def _goto(self, target, label):
        d = self._dist(target)
        if d <= self.arrive_radius:
            return f"уже на {label} ({d:.1f}м)"
        self.travel(self.bot, target)
        return f"телепорт на {label}, было {d:.0f}м"

    def _take_menu(self, *needles, window=MENU_WAIT):
        """Взять из очереди меню, где есть нужный пункт.

        Смотрим и диалоги (основной путь), и JSON-меню iid=10 — если сервер
        когда-нибудь переедет на интерфейсы, меню не потеряется.
        """
        now = time.time()
        for source in (self.dialogs, self.menus):
            while source and now - source[0]["at"] > window:
                source.popleft()
            for entry in list(source):
                if find_item(entry["items"], *needles) is not None:
                    source.remove(entry)
                    return entry
        return None

    def _peek_menu(self, *needles):
        """Посмотреть, есть ли меню с нужным пунктом, не забирая его."""
        for source in (self.dialogs, self.menus):
            for entry in source:
                if find_item(entry["items"], *needles) is not None:
                    return entry
        return None

    def _answer_menu(self, entry, index) -> bool:
        """Ответить на меню тем транспортом, которым оно пришло."""
        if "dialog_id" in entry:
            return self._answer(entry["dialog_id"], 1, index)
        return self.answer(index, entry["items"])

    def _fresh_dialog(self, title_contains, window=180.0):
        if not self.dialog:
            return None
        if time.time() - self.dialog_at > window:
            return None
        if title_contains and title_contains.lower() not in str(self.dialog["title"]).lower():
            return None
        return self.dialog

    def _send_menu_answer(self, index: int, items) -> bool:
        """Ответ на JSON-меню iid=10.

        Пункт передаём и индексом, и идентификатором, если сервер его дал:
        какой из них читает сервер — выясняется экспериментом, но лишнее
        поле он игнорирует, а недостающее ломает выбор.
        """
        session = getattr(self.bot, "session", None)
        if session is None:
            return False
        item_id = None
        if 0 <= index < len(items) and isinstance(items[index], dict):
            for key in ("id", "i", "s", "v"):
                value = items[index].get(key)
                if isinstance(value, (int, str)) and not isinstance(value, bool):
                    item_id = value
                    break
        payload_item = item_id if item_id is not None else index
        try:
            return bool(
                session.send_json(
                    10,
                    {
                        "r": int(index) + 1,
                        "i": str(payload_item),
                        "l": 0,
                    },
                    packet_name="interface_dialog_menu_select",
                )
            )
        except Exception as exc:
            self.log(f"[farm] ответ меню не ушёл: {exc!r}")
            return False

    def _answer(self, dialog_id, response, list_item=0, text=""):
        ok = self.bot.session.send_dialog_response(dialog_id, response, list_item, text)
        self.log(f"[farm] ответ диалога id={dialog_id} r={response} item={list_item} -> {ok}")
        self.dialog = None
        return ok

    def _clear(self):
        self.menus.clear()
        self.dialogs.clear()
        self.dialog = None
        self.challenge = None
        self._start_sent = False

    # ---- шаги --------------------------------------------------------

    def start(self):
        self.state = S_TO_FARM
        self.started_at = time.time()
        self.cycles = 0
        self._clear()
        self._menu_deadline = time.time() + MENU_DEADLINE
        self.log(f"[farm] старт цикла, состояние {self.state}")
        return self.state

    def stop(self):
        self.state = S_IDLE
        self.minigame_active = False
        self._clear()
        self.log("[farm] остановлен")
        return self.state

    def step(self):
        """Сделать ровно один переход. Возвращает (состояние, описание)."""
        handler = {
            S_TO_FARM: self._step_to_farm,
            S_TO_NPC: self._step_to_npc,
            S_WAIT_MENU: self._step_wait_menu,
            S_PICK_START: self._step_pick_start,
            S_PICK_JOB: self._step_pick_job,
            S_CAPTCHA: self._step_captcha,
            S_TO_WORK: self._step_to_work,
            S_MINIGAME: self._step_minigame,
            S_TO_DROPOFF: self._step_to_dropoff,
            S_RETRY: self._step_retry,
        }.get(self.state)

        if handler is None:
            return self.state, f"состояние {self.state} без обработчика"

        try:
            note = handler()
        except Exception as exc:
            self.log(f"[farm] шаг упал: {exc!r}")
            # Не терминальный отказ: возвращаемся к меню, а не умираем.
            self.state = S_RETRY
            self._clear()
            return self.state, f"ошибка: {exc!r}"

        self.log(f"[farm] {self.state}: {note}")
        return self.state, note

    def _step_retry(self):
        """Восстановление после сбоя: заново к НПС и по кругу."""
        if time.time() < getattr(self, "_retry_at", 0):
            return "пауза перед повтором"
        self.state = S_TO_NPC
        self._pokes = 0
        self._menu_deadline = time.time() + MENU_DEADLINE
        return "повторяю блок меню"

    def _step_to_farm(self):
        note = self._goto(FARM_LOCATION, "точку фермы")
        self.state = S_TO_NPC
        return note

    def _step_to_npc(self):
        note = self._goto(FARM_NPC, "НПС")
        self.state = S_WAIT_MENU
        self._clear()
        self._pokes = 0
        self._menu_deadline = time.time() + MENU_DEADLINE
        return note

    def _nearest_pickup(self, radius=6.0):
        """Ближайший пикап — по нему сервер открывает меню НПС."""
        try:
            picks = self.bot.session.stream.get_all_pickups() or {}
        except Exception as exc:
            self.log(f"[farm] стрим пикапов недоступен: {exc!r}")
            return None
        pos = self._pos()
        best = None
        for pid, pk in picks.items():
            try:
                d = math.dist((pk["x"], pk["y"], pk["z"]), pos)
            except Exception:
                continue
            if d <= radius and (best is None or d < best[0]):
                best = (d, pid)
        return best

    def _step_wait_menu(self):
        entry = self._peek_menu(MENU_START_WORK, MENU_START_WORK_ALT)
        if entry is not None:
            # НЕ забираем: меню нужно следующему шагу, чтобы ответить.
            self._poked = False
            self._pokes = 0
            self.state = S_PICK_START
            return f"меню открылось, пунктов {len(entry['items'])}"

        if time.time() > self._menu_deadline:
            self.state = S_RETRY
            self._retry_at = time.time() + 5.0
            return "меню не пришло за отведённое время"

        if self._pokes >= MENU_POKES:
            self.state = S_RETRY
            self._retry_at = time.time() + 10.0
            return f"меню не пришло за {MENU_POKES} попыток подбора пикапа"

        self._pokes += 1
        found = self._nearest_pickup()
        if found is None:
            return f"рядом нет пикапа (попытка {self._pokes}/{MENU_POKES})"
        dist, pid = found
        ok = self.pickup(self.bot, pid)
        return f"подобрал пикап #{pid} ({dist:.1f}м) -> {ok}, попытка {self._pokes}/{MENU_POKES}"

    def _step_pick_start(self):
        entry = self._take_menu(MENU_START_WORK, MENU_START_WORK_ALT)
        if entry is None:
            self.state = S_WAIT_MENU
            return "меню 'Основное меню' пропало — заново открываю"
        index = find_item(entry["items"], MENU_START_WORK, MENU_START_WORK_ALT)
        if index is None:
            self.state = S_RETRY
            self._retry_at = time.time() + 5.0
            return f"в меню нет пункта про работу: {[item_text(i) for i in entry['items']]}"
        ok = self._answer_menu(entry, index)
        if not ok:
            return "ответ на меню не ушёл, пробую снова"
        self._pick_job_at = time.time()
        self.state = S_PICK_JOB
        return f"выбрал пункт #{index + 1} {item_text(entry['items'][index])!r}"

    def _step_pick_job(self):
        entry = self._take_menu(MENU_JOB_HARVESTER, MENU_JOB_HARVESTER_ALT)
        if entry is None:
            if time.time() - self._pick_job_at > MENU_WAIT:
                self.state = S_RETRY
                self._retry_at = time.time() + 5.0
                return "меню 'Выбор работы' не пришло"
            return "жду меню 'Выбор работы'"
        index = find_item(entry["items"], MENU_JOB_HARVESTER, MENU_JOB_HARVESTER_ALT)
        if index is None:
            self.state = S_RETRY
            self._retry_at = time.time() + 5.0
            return f"нет пункта 'Сборщик урожая': {[item_text(i) for i in entry['items']]}"
        ok = self._answer_menu(entry, index)
        if not ok:
            return "ответ на меню не ушёл, пробую снова"
        self._captcha_deadline = time.time() + CAPTCHA_WAIT
        self.state = S_CAPTCHA
        return f"выбрал пункт #{index + 1} {item_text(entry['items'][index])!r}"

    def _step_captcha(self):
        dlg = self._fresh_dialog(CAPTCHA_TITLE, window=120.0)
        if dlg is None:
            # Капча приходит с задержкой. Раньше бот уходил на работу сразу
            # после ответа на "Выбор работы" и проверку не проходил.
            if time.time() < getattr(self, "_captcha_deadline", 0):
                return "жду окно проверки"
            self.state = S_TO_WORK
            return "капчи не было, иду на работу"

        if self.challenge is None:
            self.challenge = captcha_mod.from_dialog(
                dlg["dialog_id"],
                dlg["title"],
                dlg["info"],
                dlg["button1"],
                dlg["button2"],
            )
            self.log(f"[farm] капча: {self.challenge!r}")

        answer = self.captcha_chain.solve(self.challenge)
        if not answer:
            self.state = S_RETRY
            self._retry_at = time.time() + 5.0
            return f"капчу не решил: {self.challenge!r}"

        self._answer(dlg["dialog_id"], 1, 0, answer)
        self.log(f"[farm] капча решена ({self.challenge.solved_by}): {answer}")
        # Без сброса на втором цикле уходил код с прошлого круга.
        self.challenge = None
        self.state = S_TO_WORK
        return f"отправил ответ капчи {answer!r}"

    def _step_to_work(self):
        note = self._goto(FARM_WORK, "точку работы")
        self.state = S_MINIGAME
        self.minigame_started_at = time.time()
        return note

    def _step_minigame(self):
        elapsed = time.time() - self.minigame_started_at
        if self.on_minigame is not None:
            done = self.on_minigame(self)
            if done:
                self.state = S_TO_DROPOFF
                self.cycles += 1
                return f"мини-игра закрыта, собрано {self.kg:.1f} кг"
            if elapsed > 180.0:
                self.state = S_TO_DROPOFF
                self.cycles += 1
                return f"мини-игра не закрылась за {elapsed:.0f}с, иду сдавать"
            return f"играю, собрано {self.kg:.1f} кг"

        if elapsed > 180.0:
            self.state = S_TO_DROPOFF
            self.cycles += 1
            return f"мини-игра не подтвердилась за {elapsed:.0f}с, иду сдавать"
        return f"мини-игра: нет обработчика, жду ({elapsed:.0f}с)"

    def _step_to_dropoff(self):
        note = self._goto(FARM_DROPOFF, "точку сдачи")
        self.state = S_TO_WORK
        return note

    # ---- состояние ---------------------------------------------------

    def status(self):
        try:
            pos = self._pos()
        except Exception:
            pos = (0.0, 0.0, 0.0)
        return {
            "state": self.state,
            "cycles": self.cycles,
            "pos": tuple(round(v, 2) for v in pos),
            "to_work": round(self._dist(FARM_WORK), 1),
            "to_dropoff": round(self._dist(FARM_DROPOFF), 1),
            "kg": round(self.kg, 2),
            "minigame": self.minigame_active,
            "menu": (self.dialogs[-1]["title"] if self.dialogs else None),
            "dialog": (self.dialog or {}).get("title"),
            "captcha": repr(self.challenge) if self.challenge else None,
        }


def _default_pickup(bot, pickup_id: int) -> bool:
    """Подбор пикапа. Меню открывает ПАРА пакетов, не один.

    SendPickup(436) без picked_up_interact(413) меню не открывает — это
    уже проверено на шахте, там шлются оба.
    """
    session = getattr(bot, "session", None)
    if session is None:
        return False
    ok = False
    try:
        bot.api.SendPickup(pickup_id)
        ok = True
    except Exception:
        pass
    try:
        bot.api.pickup_interact(pickup_id)
        ok = True
    except Exception:
        pass
    # ALT. Без нажатия взаимодействия меню НПС не открывается — на шахте
    # ровно та же пара SendPickup + SetKey.
    try:
        bot.api.SetKey(keys=1)
        time.sleep(0.2)
        bot.api.SetKey(keys=0)
        ok = True
    except Exception:
        pass
    return ok
