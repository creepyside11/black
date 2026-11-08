"""Бот шахты Black Russia.

Сценарий снят с записи `PCAPdroid_02_нояб._21_26_52.pcap` (2026-11-02,
21:26:53 — 21:39:42, сервер 80.66.82.20:5125). Каждая константа ниже —
не догадка, а конкретный кадр из этой записи, и рядом с ней стоит время.

География, как её видно в трафике:

    у шахты        (2380.63, 1657.64,   13.71)   стоим 21:30:09..21:30:28
    вход           (2378.82, 1674.25,   15.13)   последняя точка до прыжка
    интерьер       ( 522.62,  517.62, 1000.38)   сервер, 21:31:16
    переодевание   ( 540.62,  515.31, 1000.32)   пришли 21:31:55
    ползунок       ( 541.83,  535.03,  998.68)   21:32:37
    второй скип    ( 557.57,  516.68,  999.71)   21:33:00
    лента / сдача  ( 557.13,  516.24,  999.71)   21:33:50
    забой          ( 554.04,  542.82,  998.70)   21:34:29

Маршрут после скипов — не круг, а челнок между лентой и забоем, 26 метров
в одну сторону: 21:33:50 туда, 21:34:21 обратно, и так по кругу. С шагом
4.3 м/с, то есть обычная пешая скорость.

Механика интерфейсов та же, что на заводе (`logic/factory.py`), поэтому
автомат построен один в один:

    63  диалог, ответ {"bk": 0}
    31  ползунок, ответ {"t": 1} когда полоса дошла
    13  уведомления
    39  текущая цель
    26  капча BrNewCaptcha, картинка в ключе "u", ответ {"t": 0, "s": "<текст>"}

Рабочие действия — RPC 436, тело = 4 байта, номер объекта в общем пуле
(база 2048). В записи видны id=16 (закрыть диалог), id=2048 и id=2049:
2049 клиент долбит каждые 0.3 с, потому что это перетаскивание ползунка,
а не нажатие кнопки.

Ничего не отправляется, пока не выставлен armed=True. Это не украшение:
на сожжённом адресе первый же пакет ловится, а мы это уже проходили.
"""

from __future__ import annotations

import math
import time

# ---------------------------------------------------------------- точки

MINE_EXTERIOR = (2380.63, 1657.64, 13.71)
MINE_ENTRY = (2378.82, 1674.25, 15.13)
INT_SPAWN = (522.62, 517.62, 1000.38)
INT_LOCKER = (540.62, 515.31, 1000.32)
INT_SLIDER = (541.83, 535.03, 998.68)
INT_SKIP2 = (557.57, 516.68, 999.71)
ROUTE_UNLOAD = (557.13, 516.24, 999.71)
ROUTE_FACE = (554.04, 542.82, 998.70)

# Всё, что выше этой высоты, — интерьер. Улица тут 13..16, интерьер 998..1000.
INTERIOR_Z = 900.0

# ---------------------------------------------------------- интерфейсы

DIALOG_INTERFACE = 63
MINIGAME_INTERFACE = 31
NOTIFY_INTERFACE = 13
GOAL_INTERFACE = 39
CAPTCHA_INTERFACE = 26

DIALOG_NEXT = {"bk": 0}
SLIDER_DONE = {"t": 1}
SLIDER_MISS = {"s": 0}
SLIDER_HIT = {"s": 1}
CLOSE = {"c": 1}

# -------------------------------------------------------------- объекты

RPC_INTERACT = 436
OBJ_CLOSE = 16          # закрытие диалога, 21:31:15
OBJ_BUTTON_A = 2048     # 21:27:52, нажатие на объект
OBJ_SLIDER = 2049       # 21:31:52..21:32:16, перетаскивание ползунка

# Сколько держим ползунок и с каким шагом. В записи шаг 0.30..0.31 с,
# всего ~80 нажатий за 24 секунды.
SLIDER_STEP = 0.30
SLIDER_MAX_STEPS = 120

# ------------------------------------------------------------ состояния

S_OFF = "off"
S_SETTLE = "settle"        # отстой после спавна, молчим
S_TRAVEL = "travel"        # едем к шахте
S_ENTER = "enter"          # входим, ждём скачок в интерьер
S_DIALOG = "dialog"        # первый диалог, страницы
S_LOCKER = "locker"        # переодевание
S_SLIDER = "slider"        # ползунок
S_SKIP2 = "skip2"          # второй диалог
S_ROUTE = "route"          # челнок лента <-> забой
S_UNLOAD = "unload"        # сдача, возможна капча
S_DONE = "done"
S_FAILED = "failed"

# Сколько молчим после успешного спавна. Сервер прекрасно видит бота,
# который через полсекунды после появления в мире уже уехал на 2 км.
# Живой человек сначала осматривается, потом идёт.
SPAWN_SETTLE = 30.0

DIALOG_TIMEOUT = 20.0    # нет страниц столько секунд — идём дальше
ENTER_TIMEOUT = 30.0     # не прыгнули в интерьер — считаем, что вход не сработал
SLIDER_TIMEOUT = 40.0    # ползунок молчит — идём дальше
ROUTE_GIVE_UP = 600.0    # столько секунд челнока без цели — выходим


def _dist(a, b):
    """Расстояние между точками. Кортежи короче трёх — бесконечность."""
    try:
        return math.dist(tuple(a)[:3], tuple(b)[:3])
    except Exception:
        return float("inf")


def is_interior(pos):
    """Мы внутри шахты? В записи интерьер всегда выше 990 по z."""
    try:
        return float(pos[2]) > INTERIOR_Z
    except Exception:
        return False


class MineBot:
    """Шахта. Один переход за step(). Без armed=True не отправляет ничего."""

    def __init__(
        self,
        bot,
        *,
        travel=None,
        travel_indoor=None,
        log=None,
        armed=False,
        arrive_radius=2.5,
        captcha_solver=None,
        route_rounds=0,
        settle_seconds=SPAWN_SETTLE,
    ):
        self.bot = bot
        # Улица и интерьер ездят по-разному, и это не прихоть.
        #
        # Снаружи путь до шахты — километр с лишним, там телепорт осмыслен.
        # Внутри расстояния 26 метров, и живой клиент в записи проходит их
        # шагом 4.3 м/с (21:33:50 -> 21:34:29, ровно 39 секунд на плечо).
        # Телепорт на 26 метров — это то, чего не бывает: скорость выходит
        # за 20 м/с на ровном месте, и сервер такое видит.
        self.travel = travel
        self.travel_indoor = travel_indoor if travel_indoor is not None else travel
        self.log = log or (lambda msg: None)
        self.armed = bool(armed)
        self.arrive_radius = float(arrive_radius)
        # Решатель капчи: любой объект с .solve(challenge) -> str | None.
        # Ничего не решаем сами — логика картинок живёт в logic/captcha.py.
        self.captcha_solver = captcha_solver
        # 0 — челнок без ограничения, N — остановиться после N кругов.
        self.route_rounds = int(route_rounds or 0)
        # Отстой после спавна. Ноль — не ждать (только для тестов).
        self.settle_seconds = float(settle_seconds or 0.0)
        # Момент подтверждённого спавна. None — ещё не знаем.
        self.spawn_at = None

        self.state = S_OFF
        self.started_at = None
        self.step_at = None

        self.dialog_pages = 0
        self.dialog_closed = 0
        self.sliders = 0
        self.hits = 0
        self.misses = 0
        self.slider_steps = 0
        self.last_slider_at = 0.0
        self.route_legs = 0
        self.route_towards_face = True
        self.captcha_solved = 0
        self.captcha_seen = 0
        self.goal = None
        self.notifications = []
        self.loaded = 0
        self.last_error = None
        self.journal = []
        self.pending = []

    # ---- журнал --------------------------------------------------------

    def _say(self, text):
        try:
            self.log(f"[ШАХТА] {text}")
        except Exception:
            pass

    def _note(self, kind, text, payload=None):
        self.journal.append({
            "at": time.time(),
            "kind": kind,
            "text": text,
            "payload": payload,
            "armed": self.armed,
        })

    # ---- отстой после спавна -------------------------------------------

    def mark_spawn(self, at=None):
        """Запомнить момент спавна. Раньше него бот не отправляет ничего."""
        self.spawn_at = float(at) if at is not None else time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(self.spawn_at))
        self._say(
            f"спавн подтверждён в {stamp}, "
            f"молчим {self.settle_seconds:.0f} с"
        )
        self._note("spawn", f"спавн в {stamp}")

    def settle_left(self):
        """Сколько ещё молчать. Ноль — уже можно."""
        if self.spawn_at is None:
            return 0.0
        return max(0.0, self.spawn_at + self.settle_seconds - time.time())

    def is_settled(self):
        return self.settle_left() <= 0.0

    def _settle_guard(self, what):
        """Не пустить действие раньше срока. Возвращает False, если рано."""
        left = self.settle_left()
        if left <= 0.0:
            return True
        self._say(f"молчу, {what}: после спавна ещё {left:.0f} с")
        self._note("held", f"отложено «{what}»: до конца отстоя {left:.1f} с")
        return False

    # ---- журнал действий -----------------------------------------------

    def _where(self):
        pos = self._pos()
        if not pos or len(pos) < 3:
            return "(позиция неизвестна)"
        return f"({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})"

    def _action(self, what, detail=""):
        """Каждое действие — в лог, с координатами.

        Без этого в логе видно «переход на шахту», но не видно, откуда и
        на сколько метров. А разбирать потом нечем.
        """
        line = f"{what}" + (f" — {detail}" if detail else "")
        self._say(f"▶ {line} | сейчас {self._where()}")
        self._note("action", line)

    # ---- приём кадров --------------------------------------------------

    def on_receive_json(self, bot_or_iid, iid=None, data=None, *args, **kwargs):
        """Слушатель onReceiveJSON. Эмиттер зовёт как (bot, iid, data),
        поэтому принимаем и эту форму, и прямой вызов (iid, data)."""
        if iid is None:
            iid, data = bot_or_iid, iid
        self.on_interface(iid, data)

    def on_interface(self, iid, data):
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        self._note("frame", f"iid={iid} {data}", data)

        if iid == DIALOG_INTERFACE:
            self._on_dialog(data)
        elif iid == MINIGAME_INTERFACE:
            self._on_minigame(data)
        elif iid == NOTIFY_INTERFACE:
            self._on_notify(data)
        elif iid == GOAL_INTERFACE:
            self.goal = str(data.get("mq") or data.get("s") or "")
            self._say(f"цель: {self.goal!r}")
        elif iid == CAPTCHA_INTERFACE:
            self._on_captcha(data)

    def _on_dialog(self, data):
        if data.get("c"):
            self.dialog_closed += 1
            self._say(f"диалог закрыт (всего {self.dialog_closed})")
            return
        self.dialog_pages += 1
        text = str(data.get("d") or data.get("b") or "")[:60]
        self._say(f"диалог, страница {self.dialog_pages}: {text!r}")
        self._send_json(DIALOG_INTERFACE, DIALOG_NEXT, "страница диалога", delay=0.05)

    def _on_minigame(self, data):
        """Мини-игра «Раскопка руды» — интерфейс 31.

        В записи сервер присылает ДВЕ фазы, различаются ключом `g`:

            g=1  полоса: p_start, p_max, p_max_bar, p_tick, p_timer, p_title
            g=2  попадание, больше ничего

        Ключа `t` сервер не присылает никогда — это ответ КЛИЕНТА. Раньше
        я ждала `t` в кадре и поэтому не ответила бы ни на одну полосу.
        Длительность считаем по формуле из полей самой полосы, как в цехе.
        """
        from logic.factory import minigame_mode, slider_delay_ms

        if not data.get("o"):
            return
        mode = minigame_mode(data)

        if mode == 1:
            self.sliders += 1
            self.last_slider_at = time.time()
            formula = slider_delay_ms(data)
            wait = ((formula or 4000.0) / 1000.0) + 0.6
            self._say(
                f"полоса #{self.sliders} {data.get('p_title')!r}: "
                f"p_start={data.get('p_start')} p_max={data.get('p_max')} "
                f"p_timer={data.get('p_timer')} — ответ через {wait * 1000:.0f} мс"
            )
            self._send_json(MINIGAME_INTERFACE, SLIDER_DONE, "полоса дошла", delay=wait)
            return

        if mode == 2:
            self.hits += 1
            self._say(f"фаза попадания #{self.hits}")
            self._send_json(MINIGAME_INTERFACE, SLIDER_HIT, "попадание", delay=0.4)
            return

        self._say(f"интерфейс 31, режим g={mode}: {data}")

    def _on_notify(self, data):
        text = str(data.get("i") or "")
        if not text:
            return
        self.notifications.append(text)
        self._say(f"уведомление: {text!r}")
        low = text.lower()
        if "получили" in low or "заработали" in low:
            self.loaded += 1

    def _on_captcha(self, data):
        """Интерфейс 26. Картинка лежит по URL в ключе "u"."""
        self.captcha_seen += 1
        url = str(data.get("u") or "")
        self._say(f"капча #{self.captcha_seen}: url={url[:60]!r}")
        if not url:
            self._note("captcha_no_url", f"капча без картинки: {data}", data)
            return
        if self.captcha_solver is None:
            self._note("captcha_no_solver", "решателя капчи нет", data)
            return
        # Решатель ждёт не словарь, а CaptchaChallenge: капча в BR приходит
        # несколькими видами, и разбор вида живёт в logic/captcha.py, а не тут.
        try:
            from logic.captcha import from_interface

            challenge = from_interface(CAPTCHA_INTERFACE, data)
        except Exception as exc:
            self.last_error = f"капча: {type(exc).__name__}: {exc}"
            self._note("error", self.last_error, data)
            return
        if challenge is None:
            self._note("captcha_no_challenge", "кадр не похож на капчу", data)
            return
        try:
            answer = self.captcha_solver.solve(challenge)
        except Exception as exc:
            self.last_error = f"капча: {type(exc).__name__}: {exc}"
            self._note("error", self.last_error, data)
            return
        if not answer:
            self._note("captcha_failed", "решатель вернул пусто", data)
            return
        self.captcha_solved += 1
        self._send_json(
            CAPTCHA_INTERFACE,
            {"t": 0, "s": str(answer)},
            f"ответ на капчу #{self.captcha_solved}",
            delay=0.15,
        )

    # ---- отправка ------------------------------------------------------

    def _send_json(self, iid, payload, note, *, delay=0.0):
        if not self._settle_guard(f"iid={iid} {note}"):
            return False
        if not self.armed:
            self._say(f"ХОЛОСТОЙ ХОД: iid={iid} {payload} — {note}")
            self._note("would_send", f"iid={iid} {payload} — {note}", payload)
            return False
        self.pending.append((time.time() + float(delay), "json", iid, dict(payload), note))
        self._note("queued", f"iid={iid} {payload} — {note}", payload)
        return True

    def _interact(self, obj_id, label):
        """RPC 436 с номером объекта."""
        if not self._settle_guard(f"RPC {RPC_INTERACT} id={obj_id} {label}"):
            return f"{label}: рано, идёт отстой после спавна"
        if not self.armed:
            self._say(f"ХОЛОСТОЙ ХОД: RPC {RPC_INTERACT} id={obj_id} ({label})")
            self._note("would_send", f"RPC {RPC_INTERACT} id={obj_id} — {label}",
                       {"id": obj_id})
            return f"{label}: холостой ход"
        self.pending.append(
            (time.time(), "rpc", obj_id, None, label)
        )
        self._note("queued", f"RPC {RPC_INTERACT} id={obj_id} — {label}", {"id": obj_id})
        return f"{label}: в очередь"

    def _flush(self):
        if not self.armed:
            self.pending.clear()
            return
        now = time.time()
        ready = [item for item in self.pending if item[0] <= now]
        self.pending = [item for item in self.pending if item[0] > now]
        session = getattr(self.bot, "session", None)
        for _, kind, arg, payload, note in ready:
            if session is None or not getattr(session, "connected", False):
                self.last_error = "сессия не подключена"
                self._note("error", f"нет сессии: {note}", payload)
                continue
            try:
                if kind == "json":
                    ok = session.send_json(int(arg), payload)
                else:
                    from samp.packets import PacketBuilder
                    session._sender.send(
                        PacketBuilder.rpc_build("picked_up_pickup", int(arg)),
                        rpc_name="picked_up_pickup",
                    )
                    ok = True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._note("error", f"отправка упала: {self.last_error}", payload)
                self._say(f"отправка упала: {self.last_error}")
                continue
            if ok:
                self._note("sent", f"{note}", payload)
            else:
                self.last_error = "send вернул False"
                self._note("error", f"send False: {note}", payload)

    # ---- движение ------------------------------------------------------

    def _pos(self):
        try:
            return self.bot.api.getbotposition()
        except Exception:
            return None

    def _goto(self, target, label):
        pos = self._pos()
        if not pos:
            return f"позиция неизвестна, до «{label}» не иду"
        d = _dist(pos, target)
        if d <= self.arrive_radius:
            return f"на точке «{label}» ({d:.1f} м)"

        # Внутри шахты идём пешком. Признак берём по обеим точкам: если мы
        # уже в интерьере или цель в интерьере — это интерьерный переход.
        indoor = is_interior(pos) or is_interior(target)
        move = self.travel_indoor if indoor else self.travel
        where = "внутри" if indoor else "снаружи"

        if move is None:
            self._say(f"транспорт ({where}) не задан. до «{label}» {d:.0f} м")
            return f"нет транспорта ({where}) до «{label}»"

        here = tuple(pos)[:3]
        way = "шагом" if indoor else "телепортом"
        self._action(
            f"переход «{label}» ({where}, {way})",
            f"{d:.1f} м, {here[0]:.1f},{here[1]:.1f},{here[2]:.1f} -> "
            f"{target[0]:.1f},{target[1]:.1f},{target[2]:.1f}",
        )
        result = move(self.bot, target)
        after = self._pos() if result is None else result
        if after and len(after) >= 3:
            self._action(
                f"прибытие «{label}»",
                f"промах {_dist(after, target):.1f} м, "
                f"в ({after[0]:.1f}, {after[1]:.1f}, {after[2]:.1f})",
            )
        return f"переход ({where}) на «{label}», было {d:.0f} м"

    # ---- цикл ----------------------------------------------------------

    def start(self):
        self.started_at = time.time()
        self._say(
            f"бот поднят. режим {'БОЕВОЙ' if self.armed else 'ХОЛОСТОЙ'}"
        )
        self._note("start", f"armed={self.armed}")

        # Момент спавна — точка отсчёта. Если его не передали, считаем,
        # что спавн случился прямо сейчас: пусть лучше переждём лишние
        # полминуты, чем уедем с точки появления на глазах у сервера.
        if self.spawn_at is None:
            self.mark_spawn()

        if self.is_settled():
            self.state = S_TRAVEL
            self._action("старт сразу", "отстой не нужен")
        else:
            self.state = S_SETTLE
            self._action(
                "отстой после спавна",
                f"молчим {self.settle_left():.0f} с, ни одного пакета",
            )

    def stop(self, reason=""):
        self.state = S_OFF
        self.pending.clear()
        self._say(f"остановлен. {reason}".strip())
        self._note("stop", reason)

    def step(self):
        if self.state == S_OFF:
            return "бот выключен"
        self._flush()
        handler = {
            S_SETTLE: self._step_settle,
            S_TRAVEL: self._step_travel,
            S_ENTER: self._step_enter,
            S_DIALOG: self._step_dialog,
            S_LOCKER: self._step_locker,
            S_SLIDER: self._step_slider,
            S_SKIP2: self._step_skip2,
            S_ROUTE: self._step_route,
            S_UNLOAD: self._step_unload,
        }.get(self.state)
        if handler is None:
            return f"состояние {self.state} ничего не делает"
        text = handler()
        self.step_at = time.time()
        return text

    def _step_settle(self):
        """Стоим и молчим. Ни диалогов, ни RPC, ни телепорта."""
        left = self.settle_left()
        if left > 0.0:
            return f"отстой после спавна, осталось {left:.0f} с"
        self.state = S_TRAVEL
        self._action(
            "отстой закончен",
            f"прошло {self.settle_seconds:.0f} с после спавна",
        )
        return "отстой закончен, еду на шахту"

    def _step_travel(self):
        pos = self._pos()
        if pos and _dist(pos, MINE_EXTERIOR) <= self.arrive_radius:
            self._action("на шахте", "переключаюсь на вход")
            self.state = S_ENTER
            return "на шахте, иду на вход"
        text = self._goto(MINE_EXTERIOR, "шахта")
        return text

    def _step_enter(self):
        pos = self._pos()
        if pos and is_interior(pos):
            self._action("в интерьере", "жду диалог")
            self.state = S_DIALOG
            self.started_at = time.time()
            return f"в интерьере ({pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f}), жду диалог"
        if pos and _dist(pos, MINE_ENTRY) > self.arrive_radius:
            return self._goto(MINE_ENTRY, "вход")
        # На входе стоим — сервер сам закинет внутрь. Ждём.
        if self.step_at and time.time() - self.step_at > ENTER_TIMEOUT:
            self.state = S_FAILED
            return "вход не сработал, стою на точке входа"
        return "на точке входа, жду переноса в интерьер"

    def _step_dialog(self):
        if self.dialog_closed:
            self._action("диалог пройден", f"страниц {self.dialog_pages}")
            self.state = S_LOCKER
            return f"диалог пройден ({self.dialog_pages} страниц), иду переодеваться"
        if self.started_at and time.time() - self.started_at > DIALOG_TIMEOUT:
            self._action("диалога нет", "иду переодеваться")
            self.state = S_LOCKER
            return "диалога нет, иду переодеваться"
        return f"жду диалог, страниц {self.dialog_pages}"

    def _step_locker(self):
        pos = self._pos()
        if not pos or _dist(pos, INT_LOCKER) > self.arrive_radius:
            return self._goto(INT_LOCKER, "переодевание")
        text = self._interact(OBJ_SLIDER, "переодевание")
        self._action("переодевание", f"RPC {RPC_INTERACT} id={OBJ_SLIDER}")
        self.state = S_SLIDER
        self.started_at = time.time()
        self.slider_steps = 0
        return text

    def _step_slider(self):
        pos = self._pos()
        if not pos or _dist(pos, INT_SLIDER) > self.arrive_radius:
            return self._goto(INT_SLIDER, "ползунок")
        # Ползунок крутится повторными 436, пока не придёт кадр 31.
        if self.sliders and self.last_slider_at:
            if time.time() - self.last_slider_at > 3.0:
                self._action("ползунок отработал", f"полос {self.sliders}, попаданий {self.hits}")
                self.state = S_SKIP2
                return "ползунок отработал, иду ко второму диалогу"
        if self.started_at and time.time() - self.started_at > SLIDER_TIMEOUT:
            self.state = S_SKIP2
            return "ползунок молчит, иду ко второму диалогу"
        if self.slider_steps < SLIDER_MAX_STEPS:
            self.slider_steps += 1
            self._interact(OBJ_SLIDER, f"ползунок, шаг {self.slider_steps}")
            return f"кручу ползунок, шаг {self.slider_steps}"
        self.state = S_SKIP2
        return "шаги ползунка кончились, иду ко второму диалогу"

    def _step_skip2(self):
        pos = self._pos()
        if not pos or _dist(pos, INT_SKIP2) > self.arrive_radius:
            return self._goto(INT_SKIP2, "второй диалог")
        text = self._interact(OBJ_CLOSE, "закрыть второй диалог")
        self._action("второй диалог закрыт", f"RPC {RPC_INTERACT} id={OBJ_CLOSE}")
        self.state = S_ROUTE
        self.started_at = time.time()
        self.route_legs = 0
        self.route_towards_face = True
        return text

    def _step_route(self):
        if self.state != S_ROUTE:
            return "не на маршруте"
        if self.route_rounds and self.route_legs >= self.route_rounds * 2:
            self._action("кругов хватит", f"плеч {self.route_legs}")
            self.state = S_UNLOAD
            return f"кругов хватит ({self.route_rounds}), иду сдавать"
        if self.started_at and time.time() - self.started_at > ROUTE_GIVE_UP:
            self.state = S_UNLOAD
            return "челнок без результата, иду сдавать"
        target = ROUTE_FACE if self.route_towards_face else ROUTE_UNLOAD
        label = "забой" if self.route_towards_face else "лента"
        pos = self._pos()
        if pos and _dist(pos, target) <= self.arrive_radius:
            self.route_towards_face = not self.route_towards_face
            self.route_legs += 1
            self._action(f"дошёл до «{label}»", f"разворот, плечо {self.route_legs}")
            return f"дошёл до «{label}», разворот (плечо {self.route_legs})"
        return self._goto(target, label)

    def _step_unload(self):
        pos = self._pos()
        if not pos or _dist(pos, ROUTE_UNLOAD) > self.arrive_radius:
            return self._goto(ROUTE_UNLOAD, "сдача")
        if self.captcha_seen and not self.captcha_solved:
            return "на сдаче висит капча, ответить нечем"
        if self.loaded:
            self._action("сдал груз", f"зачислений {self.loaded}")
            self.state = S_DONE
            return f"сдал ({self.loaded}), работа закончена"
        if self.step_at and time.time() - self.step_at > 30:
            self.state = S_DONE
            return "на сдаче пусто, заканчиваю"
        return "на сдаче, жду капчу или зачисление"

    # ---- отчёт ---------------------------------------------------------

    def status(self):
        return {
            "состояние": self.state,
            "боевой": self.armed,
            "страниц_диалога": self.dialog_pages,
            "диалогов_закрыто": self.dialog_closed,
            "ползунков": self.sliders,
            "попаданий": self.hits,
            "шагов_ползунка": self.slider_steps,
            "плеч_маршрута": self.route_legs,
            "капч_видел": self.captcha_seen,
            "капч_решено": self.captcha_solved,
            "сдано": self.loaded,
            "цель": self.goal,
            "в_очереди": len(self.pending),
            "последняя_ошибка": self.last_error,
        }

    def dump_journal(self, limit=60):
        lines = []
        for e in self.journal[-limit:]:
            t = time.strftime("%H:%M:%S", time.localtime(e["at"]))
            lines.append(f"{t} [{e['kind']:>12}] {e['text'][:150]}")
        return "\n".join(lines)
