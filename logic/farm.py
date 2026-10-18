"""Сборщик урожая на ферме Black Russia.

Полный цикл, снятый с экрана:

    спавн
      -> точка фермы (1652.665, 1301.039, 12.119)
      -> НПС (1641.672, 1297.149, 12.140)
         меню "Основное меню"  -> пункт 3 "Начать / Закончить работу"
         меню "Выбор работы"   -> пункт 1 "Сборщик урожая"
         окно "ПРОВЕРКА"       -> капча, решается через logic.captcha
      -> точка работы (1188.963, 1267.451, 19.153)
         мини-игра "Сборщик фруктов": тапать яблоки, набрать 5.0 кг
      -> точка сдачи (1189.129, 1313.498, 18.644)
      -> снова к точке работы

Каждое состояние — отдельный шаг. step() двигает ровно на один переход и
возвращает описание, чтобы агент оставался под контролем и ничего не
проматывал вслепую.
"""

from __future__ import annotations

import math
import time

from . import captcha as captcha_mod

FARM_LOCATION = (1652.665, 1301.039, 12.119)
FARM_NPC = (1641.672, 1297.149, 12.140)
FARM_WORK = (1188.963, 1267.451, 19.153)
FARM_MINIGAME = (1202.184, 1263.875, 20.607)
FARM_DROPOFF = (1189.129, 1313.498, 18.644)

MENU_MAIN_TITLE = "Основное меню"
MENU_JOB_TITLE = "Выбор работы"
CAPTCHA_TITLE = "ПРОВЕРКА"
MINIGAME_TITLE = "Сборщик фруктов"

ITEM_START_WORK = 3
ITEM_JOB_HARVESTER = 1

MINIGAME_TARGET_KG = 5.0

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
S_DONE = "done"
S_FAILED = "failed"


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
        arrive_radius=3.0,
    ):
        self.bot = bot
        self.travel = travel
        self.pickup = pickup
        self.log = log or (lambda msg: None)
        self.captcha_chain = captcha_chain or captcha_mod.SolverChain(
            [captcha_mod.PacketTextSolver()]
        )
        self.on_minigame = on_minigame
        self.arrive_radius = float(arrive_radius)

        self.state = S_IDLE
        self.started_at = None
        self.cycles = 0

        self.dialog = None
        self.dialog_at = 0.0
        self.challenge = None
        self.minigame_active = False
        self.minigame_started_at = 0.0
        self.kg = 0.0
        self._poked = False
        self._pokes = 0

    # ---- события -----------------------------------------------------

    def on_dialog(self, dialog_id, style, title, info, button1="", button2=""):
        self.dialog = {
            "dialog_id": dialog_id,
            "style": style,
            "title": title or "",
            "info": info or "",
            "button1": button1 or "",
            "button2": button2 or "",
        }
        self.dialog_at = time.time()
        self.log(
            f"[farm] диалог id={dialog_id} style={style} title={title!r} "
            f"info={str(info)[:120]!r} b1={button1!r} b2={button2!r}"
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

    def _fresh_dialog(self, title_contains, window=180.0):
        if not self.dialog:
            return None
        if time.time() - self.dialog_at > window:
            return None
        if title_contains and title_contains.lower() not in str(self.dialog["title"]).lower():
            return None
        return self.dialog

    def _answer(self, dialog_id, response, list_item=0, text=""):
        ok = self.bot.session.send_dialog_response(dialog_id, response, list_item, text)
        self.log(f"[farm] ответ диалога id={dialog_id} r={response} item={list_item} -> {ok}")
        self.dialog = None
        return ok

    def _clear(self):
        self.dialog = None
        self.challenge = None

    # ---- шаги --------------------------------------------------------

    def start(self):
        self.state = S_TO_FARM
        self.started_at = time.time()
        self.cycles = 0
        self._clear()
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
        }.get(self.state)

        if handler is None:
            return self.state, f"состояние {self.state} без обработчика"

        try:
            note = handler()
        except Exception as exc:
            self.state = S_FAILED
            self.log(f"[farm] шаг упал: {exc!r}")
            return self.state, f"ошибка: {exc!r}"

        self.log(f"[farm] {self.state}: {note}")
        return self.state, note

    def _step_to_farm(self):
        note = self._goto(FARM_LOCATION, "точку фермы")
        self.state = S_TO_NPC
        return note

    def _step_to_npc(self):
        note = self._goto(FARM_NPC, "НПС")
        self.state = S_WAIT_MENU
        self._clear()
        return note

    def _nearest_pickup(self, radius=6.0):
        """Ближайший пикап — по нему сервер открывает меню НПС."""
        try:
            picks = self.bot.session.stream.get_all_pickups() or {}
        except Exception:
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
        dlg = self._fresh_dialog(MENU_MAIN_TITLE)
        if dlg is not None:
            self._poked = False
            self._pokes = 0
            self.state = S_PICK_START
            return f"меню открылось: {dlg['title']!r}"

        # Меню в Black Russia открывается RPC-подбором пикапа, а не самим
        # фактом присутствия. Без этого пакета диалог не придёт никогда.
        if self._pokes >= 6:
            self.state = S_FAILED
            return "меню так и не пришло за 6 попыток подбора пикапа"

        self._pokes += 1
        found = self._nearest_pickup()
        if found is None:
            return f"рядом нет пикапа (попытка {self._pokes}/6) — жду стрим"
        dist, pid = found
        if self.pickup is not None:
            self.pickup(self.bot, pid)
        return f"подобрал пикап #{pid} ({dist:.1f}м), попытка {self._pokes}/6"

    def _step_pick_start(self):
        dlg = self._fresh_dialog(MENU_MAIN_TITLE)
        if dlg is None:
            # меню закрылось/протухло — заново подходим к пикапу
            self.state = S_WAIT_MENU
            self._pokes = 0
            return "меню 'Основное меню' пропало — заново открываю"
        # list_item в SA-MP с нуля, пункт 3 -> индекс 2
        self._answer(dlg["dialog_id"], 1, ITEM_START_WORK - 1)
        self.state = S_PICK_JOB
        return f"выбрал пункт {ITEM_START_WORK} 'Начать / Закончить работу'"

    def _step_pick_job(self):
        dlg = self._fresh_dialog(MENU_JOB_TITLE)
        if dlg is None:
            self.state = S_WAIT_MENU
            self._pokes = 0
            return "меню 'Выбор работы' не пришло — заново открываю"
        self._answer(dlg["dialog_id"], 1, ITEM_JOB_HARVESTER - 1)
        self.state = S_CAPTCHA
        return f"выбрал пункт {ITEM_JOB_HARVESTER} 'Сборщик урожая'"

    def _step_captcha(self):
        dlg = self._fresh_dialog(CAPTCHA_TITLE, window=120.0)
        if dlg is None:
            # капчи нет — сразу на работу
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
            self.state = S_FAILED
            return f"капчу не решил: {self.challenge!r}"

        # в диалоге капчи ответ вводится текстом в поле
        self._answer(dlg["dialog_id"], 1, 0, answer)
        self.log(f"[farm] капча решена ({self.challenge.solved_by}): {answer}")
        self.state = S_TO_WORK
        return f"отправил ответ капчи {answer!r}"

    def _step_to_work(self):
        note = self._goto(FARM_WORK, "точку работы")
        self.state = S_MINIGAME
        self.minigame_started_at = time.time()
        return note

    def _step_minigame(self):
        if self.on_minigame is not None:
            done = self.on_minigame(self)
            if done:
                self.state = S_TO_DROPOFF
                self.cycles += 1
                return f"мини-игра закрыта, собрано {self.kg:.1f} кг"
            return f"играю, собрано {self.kg:.1f} кг"

        elapsed = time.time() - self.minigame_started_at
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
        pos = self._pos()
        return {
            "state": self.state,
            "cycles": self.cycles,
            "pos": tuple(round(v, 2) for v in pos),
            "to_work": round(self._dist(FARM_WORK), 1),
            "to_dropoff": round(self._dist(FARM_DROPOFF), 1),
            "kg": round(self.kg, 2),
            "minigame": self.minigame_active,
            "dialog": (self.dialog or {}).get("title"),
            "captcha": repr(self.challenge) if self.challenge else None,
        }
