"""Поднятие предметов с земли — сборщик по маршруту.

Протокол снят с живого клиента: поднятие предмета с земли — это ОДИН
пакет RPC 413 с телом-константой 18 (0b9df10090000000). Id предмета в
пакете нет: сервер сам решает, что лежит под игроком. Значит боту не
нужно знать, ЧТО он поднимает — нужно стоять рядом и нажать.

Шесть выдач в craft.pcap и pickup.pcap, все шесть с этим телом за
14–22 мс до уведомления «Вы получили ...». Разбор — PICKUP_PROTOCOL.md.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ farm.py
    farm.py и шахта работают с пикапами-МЕНЮ: там 413 идёт с id пикапа и
    открывает меню. Земля — наоборот, 413 с константой и без меню. Один
    RPC, две разные механики, и путать их нельзя.

РЕЖИМЫ
    RoutePickup — идёт по записанному маршруту и пробует поднять в каждой
    точке. Это под запись перемещений с телефона.

    HuntPickup — сам находит предметы по RPC 377 (CREATE_PICKUP) и идёт к
    ближайшему. Точнее, но зависит от того, рассылает ли сервер объекты.

По умолчанию armed=False: стенд считает и пишет журнал, но НЕ отправляет.
"""

from __future__ import annotations

import json
import math
import time

# --- протокол ---------------------------------------------------------------

INTERFACE_NOTIFY = 13          # уведомление о предмете
RPC_CREATE_PICKUP = 377        # сервер создал объект
NOTIFY_GRANT_TYPE = 5          # {"o":1,"t":5,...} — «Вы получили ...»

GRANT_MARKERS = (
    "вы получили",
    "подобрали",
    "вы подняли",
)

# --- политика ---------------------------------------------------------------

PICKUP_RANGE = 3.5             # на каком расстоянии жать
HUNT_RADIUS = 40.0             # докуда искать предмет в режиме охоты
TRAVEL_ARRIVE = 1.5            # когда считаем, что дошли
GRANT_TIMEOUT = 3.0            # сколько ждать уведомление
RETRY_LIMIT = 2                # сколько раз пробовать одну точку

# состояния
S_OFF = "off"
S_TRAVEL = "travel"
S_PICKUP = "pickup"
S_WAIT = "wait"
S_DONE = "done"


def distance(a, b) -> float:
    return math.dist(tuple(a), tuple(b))


def is_grant(interface_id: int, data: dict) -> bool:
    """Похоже ли это на «вы получили предмет»."""
    if interface_id != INTERFACE_NOTIFY or not isinstance(data, dict):
        return False
    if data.get("t") != NOTIFY_GRANT_TYPE:
        return False
    text = str(data.get("i", "")).lower()
    return any(m in text for m in GRANT_MARKERS)


def load_route(path: str):
    """Прочитать маршрут из JSON вида [[время, x, y, z], ...].

    Такой формат пишет наш разбор захвата (см. _diag/route_arzamas_farm.json).
    Возвращает список (x, y, z). Время сохраняем отдельно — по нему можно
    потом воспроизвести темп, но для ходьбы он не нужен.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    points = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 4:
            continue
        try:
            points.append((float(row[1]), float(row[2]), float(row[3])))
        except (TypeError, ValueError):
            continue
    return thin_route(points)


def thin_route(points, min_step: float = 3.0):
    """Выкинуть точки, до которых и так дойдём.

    Запись идёт по 4 точки в секунду, и на прямой это сотни почти
    одинаковых координат. Для ходьбы нужны только повороты.
    """
    if not points:
        return []
    out = [points[0]]
    for p in points[1:]:
        if distance(out[-1], p) >= min_step:
            out.append(p)
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def describe(data: dict) -> str:
    if not isinstance(data, dict):
        return f"непонятное: {data!r}"
    if "c" in data:
        return "закрытие"
    text = data.get("i") or data.get("mq") or data.get("d") or ""
    return str(text)[:70]


class GroundPickup:
    """Сборщик предметов с земли. По умолчанию не отправляет ничего."""

    def __init__(
        self,
        bot,
        *,
        route=None,
        mode="route",
        armed: bool = False,
        travel=None,
        log=None,
        pickup_range: float = PICKUP_RANGE,
        hunt_radius: float = HUNT_RADIUS,
        grant_timeout: float = GRANT_TIMEOUT,
        retry_limit: int = RETRY_LIMIT,
    ):
        self.bot = bot
        self.mode = mode
        self.armed = bool(armed)
        self.travel = travel
        self.log = log
        self.pickup_range = float(pickup_range)
        self.hunt_radius = float(hunt_radius)
        self.grant_timeout = float(grant_timeout)
        self.retry_limit = int(retry_limit)

        self.route = list(route or [])
        self.state = S_OFF
        self.journal = []
        self.started_at = None

        # что уже пробовали
        self.tries = {}            # ключ точки -> сколько раз
        self.taken = set()         # ключи, где получилось
        self.failed = set()        # ключи, где исчерпали попытки

        # живые объекты с сервера
        self.objects = {}          # id -> {"model":int,"pos":(x,y,z),"at":float}

        # ожидание уведомления
        self.sent_at = None
        self.sent_key = None
        self.sent_target = None

        # счётчики
        self.attempts = 0
        self.grants = 0
        self.misses = 0
        self.walked = 0.0
        self.last_grant = None
        self.last_grant_at = None
        self.rounds = 0

    # --- журнал ------------------------------------------------------------

    def _note(self, kind, text, payload=None):
        self.journal.append(
            {
                "at": time.time() - (self.started_at or time.time()),
                "kind": kind,
                "text": text,
                "payload": payload,
                "armed": self.armed,
            }
        )

    def _say(self, text):
        self._note("say", text)
        if self.log:
            try:
                self.log(text)
            except Exception:
                pass

    # --- слушатели ---------------------------------------------------------

    def on_receive_json(self, bot, interface_id, data):
        """Подпись под эмиттер onReceiveJSON (bot, iid, data)."""
        self.on_interface(interface_id, data)

    def on_interface(self, interface_id, data):
        # Состояние здесь НЕ трогаем — переходы ведёт только step().
        try:
            iid = int(interface_id)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        if is_grant(iid, data):
            self.grants += 1
            self.last_grant = str(data.get("i", ""))
            self.last_grant_at = time.time()
            self._say(f"получено: {self.last_grant}")
            if self.state == S_WAIT:
                self._resolve(True)
            return

        if iid == INTERFACE_NOTIFY:
            self._note("notify", f"iid=13 {describe(data)}", data)

    def on_create_pickup(self, obj_id, model, pos):
        """Сервер создал объект — запоминаем как возможную цель."""
        try:
            oid = int(obj_id)
            xyz = (float(pos[0]), float(pos[1]), float(pos[2]))
        except (TypeError, ValueError, IndexError):
            return
        self.objects[oid] = {"model": int(model), "pos": xyz, "at": time.time()}
        self._note("object", f"объект {oid} модель {model} {xyz}")

    def on_packet(self, bot, packet):
        """Подпись под эмиттер onPacket: вытаскиваем create_pickup."""
        try:
            if not isinstance(packet, dict) or packet.get("rpc_id") != RPC_CREATE_PICKUP:
                return
            payload = packet.get("payload") or b""
            if len(payload) < 24:
                return
            import struct

            oid, model, _typ = struct.unpack_from("<iii", payload, 0)
            xyz = struct.unpack_from("<fff", payload, 12)
            self.on_create_pickup(oid, model, xyz)
        except Exception:
            pass

    # --- цели --------------------------------------------------------------

    def _key(self, point) -> str:
        return f"{point[0]:.1f},{point[1]:.1f},{point[2]:.1f}"

    def _pos(self):
        try:
            p = self.bot.api.getbotposition()
            return (float(p[0]), float(p[1]), float(p[2]))
        except Exception:
            return None

    def _next_route_target(self):
        for point in self.route:
            key = self._key(point)
            if key in self.taken or key in self.failed:
                continue
            if self.tries.get(key, 0) >= self.retry_limit:
                self.failed.add(key)
                continue
            return point, key
        return None, None

    def _next_hunt_target(self):
        here = self._pos()
        if here is None:
            return None, None
        best = None
        best_d = None
        for oid, info in self.objects.items():
            key = f"obj:{oid}"
            if key in self.taken or key in self.failed:
                continue
            d = distance(here, info["pos"])
            if d > self.hunt_radius:
                continue
            if best_d is None or d < best_d:
                best, best_d = (info["pos"], oid), d
        if best is None:
            return None, None
        return best[0], f"obj:{best[1]}"

    def _poll_objects(self):
        """Забрать объекты из потока сессии.

        Менеджеры уже разбирают CREATE_PICKUP и ведут список пикапов, так
        что свой разбор нужен только как подстраховка. Заодно ловим момент,
        когда объект ИСЧЕЗ из мира: для нас это подтверждение поднятия.
        """
        try:
            picks = self.bot.session.stream.get_all_pickups() or {}
        except Exception:
            return
        live = set()
        for pid, pk in picks.items():
            try:
                oid = int(pid)
                pos = (float(pk["x"]), float(pk["y"]), float(pk["z"]))
            except Exception:
                continue
            live.add(oid)
            if oid not in self.objects:
                self.objects[oid] = {"model": 0, "pos": pos, "at": time.time()}
        for oid in list(self.objects):
            if oid in live:
                continue
            key = f"obj:{oid}"
            if key in self.tries and key not in self.taken and key not in self.failed:
                self.taken.add(key)
                self._note("gone", f"объект {oid} исчез после нажатия — поднят")
            del self.objects[oid]

    def on_receive_rpc(self, bot, rpc_id, payload):
        """Подпись под эмиттер onReceiveRPC (bot, rpc_id, payload)."""
        try:
            if int(rpc_id) != RPC_CREATE_PICKUP:
                return
        except (TypeError, ValueError):
            return
        self.on_packet(None, {"rpc_id": RPC_CREATE_PICKUP, "payload": payload})

    def _next_target(self):
        if self.mode == "hunt":
            return self._next_hunt_target()
        point, key = self._next_route_target()
        if point is None:
            # маршрут кончился — добираем то, что сервер сам прислал
            return self._next_hunt_target()
        return point, key

    # --- отправка ----------------------------------------------------------

    def _send_pickup(self, key, target) -> bool:
        """Один пакет — константа 18. Id предмета не нужен."""
        self.attempts += 1
        text = f"поднятие: RPC 413 тело 18 (ключ {key})"
        if not self.armed:
            self._say(f"ХОЛОСТОЙ ХОД: {text}")
            self._note("would_send", text)
            self.taken.add(key)
            return False
        try:
            ok = self.bot.api.pickup_ground()
        except Exception as exc:
            self._note("error", f"pickup_ground упал: {exc!r}")
            return False
        if not ok:
            self._note("error", "pickup_ground вернул False")
            return False
        self._note("sent", text)
        self.sent_at = time.time()
        self.sent_key = key
        self.sent_target = target
        self.tries[key] = self.tries.get(key, 0) + 1
        return True

    def _resolve(self, granted: bool):
        key = self.sent_key
        if granted:
            if key:
                self.taken.add(key)
            self._say("предмет поднят" if not key else f"поднят (ключ {key})")
        else:
            self.misses += 1
            if key and self.tries.get(key, 0) >= self.retry_limit:
                self.failed.add(key)
                self._say(f"точка {key} не далась, больше не пробую")
            else:
                self._say("уведомления нет, попробую ещё")
        self.sent_at = None
        self.sent_key = None
        self.sent_target = None
        self.state = S_TRAVEL

    # --- цикл --------------------------------------------------------------

    def start(self):
        self.started_at = time.time()
        self.state = S_TRAVEL
        self._say(
            f"сборщик запущен: режим {self.mode}, armed={self.armed}, "
            f"точек маршрута {len(self.route)}"
        )
        return self.state

    def stop(self, why=""):
        self.state = S_OFF
        self._say(f"сборщик остановлен: {why}" if why else "сборщик остановлен")

    def next_round(self):
        """Начать круг заново: предметы на земле появляются снова."""
        self.taken.clear()
        self.failed.clear()
        self.tries.clear()
        self.sent_at = None
        self.sent_key = None
        self.sent_target = None
        self.rounds = getattr(self, "rounds", 0) + 1
        self.state = S_TRAVEL
        return self.rounds

    def status(self):
        return {
            "state": self.state,
            "armed": self.armed,
            "режим": self.mode,
            "попыток": self.attempts,
            "поднято": self.grants,
            "без ответа": self.misses,
            "объектов от сервера": len(self.objects),
            "осталось точек": max(0, len(self.route) - len(self.taken) - len(self.failed)),
            "последний": self.last_grant,
        }

    def step(self):
        if self.state == S_OFF:
            return "сборщик выключен"

        now = time.time()
        self._poll_objects()

        if self.state == S_WAIT:
            waited = now - (self.sent_at or now)
            if waited >= self.grant_timeout:
                self._resolve(False)
            return f"ждём уведомление, {waited:.1f} с"

        target, key = self._next_target()
        if target is None:
            self.state = S_DONE
            self._say(
                f"всё: поднято {self.grants}, без ответа {self.misses}, "
                f"попыток {self.attempts}"
            )
            return "работа закончена"

        here = self._pos()
        if here is None:
            return "нет позиции бота"

        d = distance(here, target)
        if d > self.pickup_range:
            self.state = S_TRAVEL
            if self.travel is None:
                return f"нет транспорта, до цели {d:.1f} м"
            try:
                self.travel(self.bot, target)
            except Exception as exc:
                self._note("error", f"переход упал: {exc!r}")
                self.failed.add(key)
                return f"переход упал: {exc!r}"
            self.walked += d
            return f"иду к {target}, осталось {d:.1f} м"

        self.state = S_PICKUP
        if not self._send_pickup(key, target):
            # холостой ход: цель считаем отработанной, идём дальше
            return "холостой ход, отправки нет"
        self.state = S_WAIT
        return f"нажала на {target}, жду уведомление"

    # --- отчёт -------------------------------------------------------------

    def report(self) -> dict:
        if self.attempts == 0:
            verdict = "недостаточно данных"
        elif self.grants == 0:
            verdict = "ни один предмет не поднялся — тело 18 не сработало"
        elif self.misses == 0:
            verdict = "работает: каждый пакет дал предмет"
        else:
            verdict = f"работает частично: {self.grants} из {self.attempts}"
        return {
            "попыток": self.attempts,
            "поднято": self.grants,
            "без ответа": self.misses,
            "пройдено_м": round(self.walked, 1),
            "вердикт": verdict,
        }

    def dump_journal(self) -> str:
        lines = ["=== СБОР С ЗЕМЛИ ==="]
        for e in self.journal:
            if e["kind"] in ("say", "sent", "would_send", "error"):
                lines.append(f"  {e['at']:7.2f}  {e['text']}")
        lines.append("=== конец ===")
        return "\n".join(lines)
