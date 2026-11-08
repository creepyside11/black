"""Завод Black Russia — ТЕСТОВЫЙ СТЕНД. НЕ РАБОЧИЙ БОТ.

##############################################################################
#                                                                            #
#   ЭТО НЕ БОЕВОЙ МОДУЛЬ И НЕ ГОТОВЫЙ БОТ.                                   #
#                                                                            #
#   Стенд для опытов и проверок гипотез. Он намеренно ничего не отправляет,  #
#   пока его не взведут флагом armed=True, и по умолчанию только слушает и   #
#   пишет журнал. Половина шагов ниже — предположения, не проверенные на     #
#   живом сервере. В боевой цикл не подключать.                              #
#                                                                            #
##############################################################################

ЧТО ПОДТВЕРЖДЕНО (снято с _diag/factory.pcap, 31.10, сессия 11:17–11:21)
    Диалог входа на завод идёт потоками, интерфейс 63, а не локально.
        к нам  {"o":1,"n":"Прораб - Жора","d":"...","b":[...]}
        к нам  {"t":1, ...}                следующая страница
        ОТ НАС {"bk":0}                    ответ на страницу
        к нам  {"c":1}                     закрытие
        В захвате 6 страниц и 6 ответов. Последний ответ сервер принимает
        как «хватит» и закрывает диалог сам.

    Ползунок и попадание — интерфейс 31, ровно как описано в logic/minigames.
        g=1 -> UILayoutMiniGameEventsGameUnTie, полоса прогресса
               поля p_start, p_max, p_max_bar, p_tick, p_timer, p_title
        ОТ НАС  {"t":1}   когда полоса дошла до p_max
        g=2 -> UILayoutMiniGameEventsGameFinger, полоса с попаданием
        ОТ НАС  {"s":1} удача, {"s":0} неудача

    Замеренный тайминг {"t":1} от открытия полосы:
        фаза 0->50   4892, 5122, 4390, 4459 мс
        фаза 50->100 4171, 4085, 4085, 4152 мс
        Формула (p_max - p_start) * p_timer / p_tick даёт 4000 мс
        на обе фазы. Лишние 400–1100 мс — накладные клиента.

    Рабочие действия — RPC 436 с id объекта из CREATE_PICKUP (RPC 377).
        2048 раздевалка, 2049 сырьё, 2 вход.

    Маршрут внутри интерьера z=1381.1, сверен с трассой перемещения.

ЧТО НЕ ПРОВЕРЕНО — И РАДИ ЧЕГО ЭТОТ СТЕНД
    1. Накажет ли сервер за ранний {"t":1}. Клиент шлёт его, когда полоса
       дошла. Что будет, если послать сразу — неизвестно. Это ОПЫТ №1.
    2. Что делать с {"s":0}. Один провал из семи у живого игрока: сервер
       прислал второй g=2, клиент дослал {"s":1}. Логика повтора неясна.
    3. Точные координаты станка и ленты — это объекты карты, а не пикапы,
       в CREATE_PICKUP их нет. Взяты из трассы, погрешность пара метров.

Использование (только на тестовом аккаунте):

    from logic.factory import FactoryTester, TIMING_INSTANT
    stand = FactoryTester(bot, log=print, armed=True, timing=TIMING_INSTANT)
    # on() — декоратор и падает на связанном методе; subscribe умеет.
    emitter.subscribe("onReceiveJSON", stand.on_receive_json, sync=True)
    # и звать stand.step() из общего цикла

Опыт №1, проверка раннего "t":
    stand = FactoryTester(bot, log=print, armed=True, timing=TIMING_INSTANT)
    ... дойти до станка ...
    смотреть stand.journal: если сервер проглотил и прислал g=2 — он не
    проверяет тайминг, и бот может слать {"t":1} мгновенно.
"""

from __future__ import annotations

import math
import time

# --- интерфейсы -------------------------------------------------------------

DIALOG_INTERFACE = 63       # диалог прораба, поток страниц
MINIGAME_INTERFACE = 31     # полоса и попадание
NOTIFY_INTERFACE = 13       # уведомления "Вы получили ..."
GOAL_INTERFACE = 39         # текущая цель

# --- что отправляем ---------------------------------------------------------

DIALOG_NEXT = {"bk": 0}     # следующая страница диалога
SLIDER_DONE = {"t": 1}      # полоса дошла
HIT = {"s": 1}              # попадание
MISS = {"s": 0}             # промах
CLOSE = {"c": 1}            # закрыть

# --- объекты (id из CREATE_PICKUP) ------------------------------------------

OBJ_ENTRY = 2
OBJ_LOCKER = 2048
OBJ_MATERIAL = 2049

RPC_INTERACT = 436          # рабочие действия на заводе

# --- маршрут, интерьер z = 1381.1 -------------------------------------------

ENTER = (-29.9, -3.8, 1381.1)
LOCKER = (-21.4, -18.3, 1381.1)
MATERIAL = (-1.4, -15.1, 1380.7)
MACHINE = (-5.5, -6.4, 1381.1)
BELT = (-9.2, 22.5, 1381.1)

ROUTE_NAMES = {
    "вход": ENTER,
    "раздевалка": LOCKER,
    "сырьё": MATERIAL,
    "станок": MACHINE,
    "лента": BELT,
}

# --- тайминги ответа на полосу ----------------------------------------------

TIMING_NATURAL = "natural"   # как живой клиент: формула плюс накладные
TIMING_FAST = "fast"         # половина от формулы — проверка снизу
TIMING_INSTANT = "instant"   # сразу — ОПЫТ №1
TIMING_OFF = "off"           # не отвечать вообще, только смотреть

# Накладные клиента поверх формулы. Замерено 400–1100 мс, берём середину.
CLIENT_OVERHEAD = 0.6

# --- состояния --------------------------------------------------------------

S_OFF = "off"
S_DIALOG = "dialog"
S_LOCKER = "locker"
S_MATERIAL = "material"
S_MACHINE = "machine"
S_BELT = "belt"
S_DONE = "done"
S_FAILED = "failed"


def slider_delay_ms(json_data):
    """Сколько миллисекунд клиент ждёт перед {"t":1}. None — если не полоса.

    Формула из полей самой полосы: (p_max - p_start) * p_timer / p_tick.
    На захвате даёт ровно 4000 мс на обе фазы.
    """
    if not isinstance(json_data, dict):
        return None
    try:
        start = float(json_data.get("p_start") or 0)
        peak = float(json_data.get("p_max") or 0)
        tick = float(json_data.get("p_tick") or 1) or 1.0
        timer = float(json_data.get("p_timer") or 0)
    except (TypeError, ValueError):
        return None
    if timer <= 0:
        return None
    return max(0.0, peak - start) * timer / tick


def minigame_mode(json_data):
    """Режим из ключа `g`. None — если это не мини-игра."""
    if not isinstance(json_data, dict):
        return None
    value = json_data.get("g")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_frame(iid, data):
    """Короткая подпись кадра для журнала."""
    if not isinstance(data, dict):
        return str(data)[:120]
    if iid == DIALOG_INTERFACE:
        if data.get("c"):
            return "диалог: закрытие"
        who = data.get("n") or "?"
        text = str(data.get("d") or data.get("b") or "")[:50]
        kind = "открытие" if data.get("o") else "страница"
        return f"диалог: {kind}, {who}: {text!r}"
    if iid == MINIGAME_INTERFACE:
        mode = minigame_mode(data)
        if data.get("o"):
            delay = slider_delay_ms(data)
            tail = f", ждать {delay:.0f} мс" if delay else ""
            title = str(data.get("p_title") or "")[:30]
            return f"мини-игра: g={mode}, открытие, {title!r}{tail}"
        return f"мини-игра: g={mode}, {data}"
    if iid == NOTIFY_INTERFACE:
        return f"уведомление: {str(data.get('i') or '')[:60]!r}"
    if iid == GOAL_INTERFACE:
        return f"цель: {str(data.get('mq') or '')[:60]!r}"
    return str(data)[:120]


class FactoryTester:
    """Стенд завода. Один переход за step(). Ничего не шлёт без armed=True."""

    def __init__(
        self,
        bot,
        *,
        travel=None,
        log=None,
        armed=False,
        timing=TIMING_NATURAL,
        arrive_radius=2.5,
        material_goal=3,
        overhead=CLIENT_OVERHEAD,
        miss_every=0,
    ):
        self.bot = bot
        self.travel = travel
        self.log = log or (lambda msg: None)
        self.armed = bool(armed)
        self.timing = timing
        self.arrive_radius = float(arrive_radius)
        self.material_goal = int(material_goal)
        self.overhead = float(overhead)
        # 0 — всегда попадание. N — каждый N-й раз шлём {"s":0} намеренно,
        # чтобы увидеть реакцию сервера на промах (опыт №2).
        self.miss_every = int(miss_every or 0)

        self.state = S_OFF
        self.started_at = None
        self.cycles = 0

        # Отложенные отправки: (когда, интерфейс, тело, пометка).
        # Список, а не поток — так стенд остаётся детерминированным и
        # проверяемым тестом.
        self.pending = []
        # Журнал всего, что видели и отправили. Основа для разбора опыта.
        self.journal = []

        self.dialog_pages = 0
        self.dialog_seen = False
        self.dialog_closed = False
        self.sliders = 0
        self.hits = 0
        self.misses = 0
        self.notifications = []
        self.goal = None
        self.last_slider_at = 0.0
        self.last_error = None

    # ---- журнал --------------------------------------------------------

    def _say(self, text):
        try:
            self.log(f"[ЗАВОД] {text}")
        except Exception:
            pass

    def _note(self, kind, text, payload=None):
        self.journal.append(
            {
                "at": time.time(),
                "kind": kind,
                "text": text,
                "payload": payload,
                "armed": self.armed,
            }
        )

    # ---- приём кадров --------------------------------------------------

    def on_receive_json(self, bot_or_iid, iid=None, data=None, *args, **kwargs):
        """Обработчик события onReceiveJSON.

        Эмиттер зовёт как (bot, iid, data), поэтому принимаем обе формы:
        стенд можно подключить и напрямую, и как слушателя.
        """
        if iid is None:
            iid, data = bot_or_iid, iid
        self.on_interface(iid, data)

    def on_interface(self, iid, data):
        """Единая точка входа: разложить кадр по интерфейсу."""
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        label = describe_frame(iid, data)
        self._note("frame", f"iid={iid} {label}", data)

        if iid == DIALOG_INTERFACE:
            self._on_dialog(data)
        elif iid == MINIGAME_INTERFACE:
            self._on_minigame(data)
        elif iid == NOTIFY_INTERFACE:
            self._on_notify(data)
        elif iid == GOAL_INTERFACE:
            self.goal = str(data.get("mq") or "")
            self._say(f"цель: {self.goal!r}")

    def _on_dialog(self, data):
        if data.get("c"):
            self.dialog_closed = True
            self._say(f"диалог закрыт, страниц пройдено {self.dialog_pages}")
            return
        self.dialog_seen = True
        self.dialog_pages += 1
        text = str(data.get("d") or data.get("b") or "")[:60]
        self._say(f"диалог, страница {self.dialog_pages}: {text!r}")
        self._send(DIALOG_INTERFACE, DIALOG_NEXT, "ответ на страницу диалога", delay=0.05)

    def _on_minigame(self, data):
        if not data.get("o"):
            return
        mode = minigame_mode(data)
        delay = slider_delay_ms(data)

        if mode == 1:
            # Состояние здесь НЕ трогаем. Кадр может прийти когда угодно, а
            # переходы ведёт только step() — иначе пришедшая полоса сбивала
            # бы автомат с толку (ловилось тестом на диалог).
            self.sliders += 1
            self.last_slider_at = time.time()
            self._say(
                f"полоса #{self.sliders}: {data.get('p_title')!r}, "
                f"p_start={data.get('p_start')} p_max={data.get('p_max')} "
                f"p_timer={data.get('p_timer')}, формула {delay} мс"
            )
            self._send(
                MINIGAME_INTERFACE,
                SLIDER_DONE,
                f"полоса #{self.sliders} дошла",
                delay=self._slider_wait(delay),
            )
            return

        if mode == 2:
            # Иногда шлём промах намеренно: надо увидеть, что сервер делает
            # с {"s":0} — в захвате он прислал второй g=2 и клиент дослал
            # {"s":1}. Логика повтора непонятна, и это опыт №2.
            miss = bool(self.miss_every) and (self.hits + self.misses + 1) % self.miss_every == 0
            if miss:
                self.misses += 1
                self._say("полоса попадания: шлём ПРОМАХ намеренно (опыт №2)")
                self._send(
                    MINIGAME_INTERFACE,
                    MISS,
                    "промах намеренно",
                    delay=self._hit_wait(),
                )
                return
            self.hits += 1
            self._say(f"полоса попадания: попадание #{self.hits}")
            self._send(
                MINIGAME_INTERFACE,
                HIT,
                "попадание",
                delay=self._hit_wait(),
            )
            return

        self._say(f"интерфейс 31, неизвестный режим g={mode}: {data}")

    def _on_notify(self, data):
        text = str(data.get("i") or "")
        if not text:
            return
        self.notifications.append(text)
        self._say(f"уведомление: {text!r}")
        if "получили" in text.lower():
            self.cycles += 1

    # ---- тайминги ------------------------------------------------------

    def _slider_wait(self, formula_ms):
        """Пауза перед {"t":1} по выбранной политике."""
        if self.timing == TIMING_OFF:
            self._say("политика off — на полосу не отвечаю")
            return None
        if formula_ms is None:
            formula_ms = 4000.0
        if self.timing == TIMING_INSTANT:
            return 0.0
        if self.timing == TIMING_FAST:
            return formula_ms / 2000.0
        return (formula_ms / 1000.0) + self.overhead

    def _hit_wait(self):
        if self.timing == TIMING_OFF:
            return None
        if self.timing == TIMING_INSTANT:
            return 0.0
        return 0.4

    # ---- отправка ------------------------------------------------------

    def _send(self, iid, payload, note, *, delay=0.0):
        """Положить отправку в очередь. Без armed=True не отправляем.

        В холостом ходе всё равно считаем и показываем паузу: смысл стенда
        в том, чтобы увидеть, ЧТО и КОГДА ушло бы, не отправляя этого.
        """
        if delay is None:
            self._note("skip", f"iid={iid} не отправлено: {note}", payload)
            return False
        text = f"iid={iid} {payload} через {delay * 1000:.0f} мс — {note}"
        if not self.armed:
            self._say(f"ХОЛОСТОЙ ХОД: {text}")
            self._note("would_send", text, payload)
            return False
        self.pending.append((time.time() + float(delay), iid, dict(payload), note))
        self._note("queued", text, payload)
        return True

    def _flush(self):
        """Отправить всё, чей срок пришёл."""
        if not self.armed:
            self.pending.clear()
            return
        now = time.time()
        ready = [item for item in self.pending if item[0] <= now]
        self.pending = [item for item in self.pending if item[0] > now]
        session = getattr(self.bot, "session", None)
        for _, iid, payload, note in ready:
            if session is None or not getattr(session, "connected", False):
                self.last_error = "сессия не подключена"
                self._note("error", f"нет сессии, потеряно: {note}", payload)
                continue
            try:
                ok = session.send_json(iid, payload)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._note("error", f"отправка упала: {self.last_error}", payload)
                self._say(f"отправка упала: {self.last_error}")
                continue
            if ok:
                self._note("sent", f"iid={iid} {payload} — {note}", payload)
                self._say(f"отправлено iid={iid} {payload} ({note})")
                if payload == SLIDER_DONE:
                    self._note(
                        "probe",
                        f"шли t=1 по политике {self.timing}, через "
                        f"{(time.time() - self.last_slider_at) * 1000:.0f} мс "
                        f"после открытия полосы",
                        payload,
                    )
            else:
                self.last_error = "send_json вернул False"
                self._note("error", f"send_json False: {note}", payload)

    # ---- движение ------------------------------------------------------

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
            return f"уже на точке «{label}» ({d:.1f} м)"
        if self.travel is None:
            self._say(f"транспорт не задан, стою на месте. до «{label}» {d:.0f} м")
            return f"нет транспорта до «{label}»"
        self.travel(self.bot, target)
        return f"переход на «{label}», было {d:.0f} м"

    def _interact(self, obj_id, label):
        """RPC 436 с id объекта."""
        api = getattr(self.bot, "api", None)
        if api is None:
            return f"нет api, {label} пропущено"
        if not self.armed:
            self._say(f"ХОЛОСТОЙ ХОД: RPC {RPC_INTERACT} id={obj_id} ({label})")
            self._note("would_send", f"RPC {RPC_INTERACT} id={obj_id} — {label}", {"id": obj_id})
            return f"{label}: холостой ход"
        try:
            sender = self.bot.session._sender.send
            from samp.packets import PacketBuilder

            sender(
                PacketBuilder.rpc_build("picked_up_pickup", int(obj_id)),
                rpc_name="picked_up_pickup",
            )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._note("error", f"RPC {RPC_INTERACT} упал: {self.last_error}", {"id": obj_id})
            return f"{label}: ошибка {self.last_error}"
        self._note("sent", f"RPC {RPC_INTERACT} id={obj_id} — {label}", {"id": obj_id})
        self._say(f"RPC {RPC_INTERACT} id={obj_id} ({label})")
        return f"{label}: отправлено"

    # ---- цикл ----------------------------------------------------------

    def start(self):
        self.state = S_DIALOG
        self.started_at = time.time()
        self._say(
            f"стенд поднят. режим {'БОЕВОЙ' if self.armed else 'ХОЛОСТОЙ'}, "
            f"политика тайминга {self.timing}"
        )
        self._note("start", f"armed={self.armed} timing={self.timing}")

    def stop(self, reason=""):
        self.state = S_OFF
        self.pending.clear()
        self._say(f"стенд остановлен. {reason}".strip())
        self._note("stop", reason)

    def step(self):
        """Один переход. Возвращает строку с тем, что сделано."""
        if self.state == S_OFF:
            return "стенд выключен"
        self._flush()
        if self.state == S_DIALOG:
            return self._step_dialog()
        if self.state == S_LOCKER:
            return self._step_locker()
        if self.state == S_MATERIAL:
            return self._step_material()
        if self.state == S_MACHINE:
            return self._step_machine()
        if self.state == S_BELT:
            return self._step_belt()
        return f"состояние {self.state} ничего не делает"

    def _step_dialog(self):
        if self.dialog_closed:
            self.state = S_LOCKER
            return "диалог пройден, иду в раздевалку"
        if self.dialog_seen:
            return "диалог идёт, жду страницы"
        if self.started_at and time.time() - self.started_at > 20:
            self._say("диалога нет 20 с — захожу на завод вслепую")
            self.state = S_LOCKER
            return "диалог не пришёл, иду дальше"
        return "жду диалог прораба"

    def _step_locker(self):
        if self._dist(LOCKER) > self.arrive_radius:
            return self._goto(LOCKER, "раздевалка")
        text = self._interact(OBJ_LOCKER, "раздевалка")
        self.state = S_MATERIAL
        return text

    def _step_material(self):
        if self._dist(MATERIAL) > self.arrive_radius:
            return self._goto(MATERIAL, "сырьё")
        text = self._interact(OBJ_MATERIAL, "сырьё")
        self.state = S_MACHINE
        return text

    def _step_machine(self):
        if self.sliders and time.time() - self.last_slider_at > 25:
            self._say("полосы молчат 25 с — считаю работу на станке законченной")
            self.state = S_BELT
            return "станок закончен, иду на ленту"
        self._goto(MACHINE, "станок")
        if self.sliders:
            return f"на станке, пройдено полос {self.sliders}, удач {self.hits}, промахов {self.misses}"
        return "встал на станок, жду полосу"

    def _step_belt(self):
        text = self._goto(BELT, "лента")
        self.state = S_MATERIAL
        return text + ", цикл замкнут"

    # ---- итог ----------------------------------------------------------

    def status(self):
        return {
            "state": self.state,
            "armed": self.armed,
            "timing": self.timing,
            "циклов": self.cycles,
            "страниц_диалога": self.dialog_pages,
            "полос": self.sliders,
            "удач": self.hits,
            "промахов": self.misses,
            "в_очереди": len(self.pending),
            "последняя_ошибка": self.last_error,
            "цель": self.goal,
        }

    def probe_report(self):
        """Разбор опыта: принял ли сервер наш тайминг.

        Смотрим, что сервер ответил после наших {"t":1}. Если после раннего
        ответа пришёл g=2 и дальше {"s":...} — тайминг не проверяется.
        """
        sent = [e for e in self.journal if e["kind"] == "sent"]
        sliders = [e for e in sent if e["payload"] == SLIDER_DONE]
        frames_31 = [e for e in self.journal if e["kind"] == "frame" and "iid=31" in e["text"]]
        verdict = "недостаточно данных"
        if sliders and frames_31:
            after = [f for f in frames_31 if f["at"] > sliders[0]["at"]]
            if after:
                verdict = "сервер принял ответ и продолжил работу"
            else:
                verdict = "после нашего ответа сервер молчит — возможно, отклонил"
        return {
            "политика": self.timing,
            "отправлено_полос": len(sliders),
            "кадров_интерфейса_31": len(frames_31),
            "вердикт": verdict,
        }

    def dump_journal(self, limit=60):
        """Журнал строками — для вставки в отчёт."""
        lines = []
        for e in self.journal[-limit:]:
            t = time.strftime("%H:%M:%S", time.localtime(e["at"]))
            lines.append(f"{t} [{e['kind']:>10}] {e['text']}")
        return "\n".join(lines)
