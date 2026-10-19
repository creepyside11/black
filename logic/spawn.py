"""Спавн на реальном вокзале Black Russia.

Механика, снятая с живого сервера:

    1. request_class  (RELIABLE / SYSTEM_PRIORITY)  -> сервер шлёт RPC 372
       SetSpawnInfo с координатами камеры кинематика (z ~ 2500) и RPC 344.
    2. spawn          (RELIABLE / SYSTEM_PRIORITY)  -> подтверждаем класс.
    3. request_class  (RELIABLE_ORDERED / MEDIUM)   -> сервер шлёт RPC 448
       SetPlayerPos с НАСТОЯЩИМИ координатами вокзала.
    4. spawn          (RELIABLE_ORDERED / MEDIUM)   -> закрепляем.

Один request_class ничего не даёт: сервер одноразово отвечает 372 и дальше
молчит, пока не пройдёт пара spawn + повторный request_class. Именно поэтому
все прошлые попытки висли в небе на z=2501.

Основной путь — меню вокзалов, если сервер его прислал:

    << iid=50 {"o": 1, "m": [1, 2]}    список вокзалов
    >> iid=50 {"t": 1, "s": 1}         выбираем первый
    << iid=50 {"s": 2}                 подтверждение
       SetPlayerPos на вокзал

Сервер шлёт меню не всегда: на части серверов (NORILSK, TVER) вместо списка
приходит только {"s": 1} — приглашение, на которое он не реагирует. Тогда
работает RPC-хендшейк выше.
"""
from __future__ import annotations

import math
import threading
import time

from samp.constants import PacketPriority, PacketReliability
from samp.interfaces import InterfaceID
from samp.packets import PacketBuilder

# --- известные вокзалы ------------------------------------------------------

SPAWNS: dict[str, tuple[float, float, float]] = {
    "Нижегородск 2": (-2152.833, 1558.348, 9.884),
    "Нижегородск 1": (-2661.548, 2000.877, 9.563),
    "Батырево": (1795.375, 2518.425, 14.538),
    "Лыткарино": (-2428.754, 203.862, 26.134),
    "Южный": (2745.431, -2436.016, 21.813),
}

# Точка преавторизации: тут висят все, кто ещё не выбрал спавн.
AUTH_ZONE = (13.703383445739746, 485.29071044921875, -20.0)
AUTH_ZONE_RADIUS = 80.0

# Выше этой высоты персонажа не бывает — это камера кинематика.
SKY_Z = 400.0

# Сколько ждать SetPlayerPos после второго request_class.
SPAWN_TIMEOUT = 18.0

# Пауза между шагами хендшейка. Меньше 2с сервер не успевает ответить.
STEP_DELAY = 4.0

# Класс один: InitGame отдаёт d_spawns_available = 1.
DEFAULT_CLASS = 0


# --- геометрия --------------------------------------------------------------

def in_sky(pos) -> bool:
    """Позиция — камера в небе, а не персонаж."""
    if not pos or len(pos) < 3:
        return False
    return float(pos[2]) > SKY_Z


def in_auth_zone(pos) -> bool:
    """Позиция в зоне преавторизации."""
    if not pos or len(pos) < 3:
        return False
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    if z < -5.0:
        return True
    if abs(x) < 0.5 and abs(y) < 0.5:
        return True
    dx, dy = x - AUTH_ZONE[0], y - AUTH_ZONE[1]
    return math.hypot(dx, dy) <= AUTH_ZONE_RADIUS


def is_spawned_position(pos) -> bool:
    """Настоящий спавн: не зона авторизации, не камера в небе, не нули."""
    if not pos or len(pos) < 3:
        return False
    if abs(float(pos[0])) < 0.01 and abs(float(pos[1])) < 0.01:
        return False
    if in_sky(pos):
        return False
    return not in_auth_zone(pos)


# Насколько далеко может быть реальный вокзал от точки в таблице.
STATION_RADIUS = 150.0


def nearest_spawn_name(pos):
    """Ближайший известный вокзал и расстояние до него.

    Возвращает (None, dist) если ближайшая точка таблицы дальше
    STATION_RADIUS — значит вокзал не из нашей таблицы, а не «почти он».
    """
    if not pos or len(pos) < 3:
        return None, float("inf")
    x, y = float(pos[0]), float(pos[1])
    best, best_d = None, float("inf")
    for name, (sx, sy, _sz) in SPAWNS.items():
        d = math.hypot(x - sx, y - sy)
        if d < best_d:
            best, best_d = name, d
    if best_d > STATION_RADIUS:
        return None, best_d
    return best, best_d


def station_name(pos) -> str:
    """Имя вокзала или внятная подпись, если он не из таблицы."""
    name, dist = nearest_spawn_name(pos)
    if name:
        return name
    if not pos or len(pos) < 3:
        return "неизвестно"
    return f"вокзал ({float(pos[0]):.0f}, {float(pos[1]):.0f})"


def remember_station(pos) -> None:
    """Запомнить вокзал, которого нет в таблице — пригодится в следующий раз."""
    if not is_spawned_position(pos):
        return
    name, dist = nearest_spawn_name(pos)
    if name is None and dist > STATION_RADIUS:
        key = f"вокзал {float(pos[0]):.0f},{float(pos[1]):.0f}"
        SPAWNS.setdefault(key, (float(pos[0]), float(pos[1]), float(pos[2])))


def _getpos(bot):
    try:
        return bot.api.getbotposition() if bot.api else None
    except Exception:
        return None


def _connected(bot) -> bool:
    session = getattr(bot, "session", None)
    return bool(session is not None and getattr(session, "connected", False))


# --- отправка RPC с явной доставкой ----------------------------------------

def _send_rpc(bot, rpc_name: str, args=(), *, reliability, priority, log=None) -> bool:
    """Отправить RPC с конкретными reliability/priority.

    Имя RPC одно, а доставка нужна разная на разных шагах — поэтому таблицу
    RPC_DELIVERY тут не трогаем, шлём напрямую.
    """
    if not _connected(bot):
        return False
    try:
        payload = PacketBuilder.rpc_build(rpc_name, *args)
        bot.session.protocol.send_with_header(
            payload, reliability, ordering_channel=0, priority=priority
        )
    except Exception as exc:
        if log:
            log(f"[SPAWN] {rpc_name} не отправлен: {exc}")
        return False
    return True


def request_class(bot, wclass: int = DEFAULT_CLASS, *, ordered: bool = False, log=None) -> bool:
    """RPC RequestClass. ordered=False -> шаг 1, ordered=True -> шаг 3.

    Неупорядоченный (шаг 1) заодно разрешает автоответ на RPC 344: только
    после нашего запроса сервер имеет право прислать spawn_response, на
    который сессия отвечает RPC Spawn. Без этого флага спонтанный 344
    (например во время регистрации) приводил к RPC Spawn посреди
    регистрации и кику античита кодом 25.
    """
    session = getattr(bot, "session", None)
    if session is not None and not ordered:
        session.spawn_handshake_requested = True
    if ordered:
        return _send_rpc(
            bot, "request_class", (int(wclass),),
            reliability=PacketReliability.RELIABLE_ORDERED,
            priority=PacketPriority.MEDIUM_PRIORITY,
            log=log,
        )
    return _send_rpc(
        bot, "request_class", (int(wclass),),
        reliability=PacketReliability.RELIABLE,
        priority=PacketPriority.SYSTEM_PRIORITY,
        log=log,
    )


def send_spawn(bot, *, ordered: bool = False, log=None) -> bool:
    """RPC Spawn. ordered=False -> шаг 2, ordered=True -> шаг 4.

    Помечаем сессию как «spawn уже отправлен», чтобы автоответ на RPC 344
    не прислал второй такой же RPC следом.
    """
    if ordered:
        sent = _send_rpc(
            bot, "spawn", (),
            reliability=PacketReliability.RELIABLE_ORDERED,
            priority=PacketPriority.MEDIUM_PRIORITY,
            log=log,
        )
    else:
        sent = _send_rpc(
            bot, "spawn", (),
            reliability=PacketReliability.RELIABLE,
            priority=PacketPriority.SYSTEM_PRIORITY,
            log=log,
        )
    if sent:
        session = getattr(bot, "session", None)
        if session is not None:
            session._rpc_spawn_queued = True
    return sent


def start_sync(bot) -> None:
    """Включить отправку player_sync, если она ещё не идёт."""
    session = getattr(bot, "session", None)
    sync = getattr(session, "sync", None) if session else None
    if sync is None:
        return
    try:
        if not sync.enabled:
            sync.start()
    except Exception:
        pass


def kick_stream(bot) -> None:
    """Пнуть стриминг, чтобы сервер прислал игроков вокруг."""
    session = getattr(bot, "session", None)
    if session is None:
        return
    for name in ("send_rpc_update_scores_and_pings",):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


# --- путь через меню --------------------------------------------------------

MENU_TIMEOUT = 20.0


def wait_spawn_menu(bot, timeout: float = MENU_TIMEOUT, *, poll: float = 0.2) -> list:
    """Ждать от сервера список вокзалов. Возвращает [] если не пришёл."""
    items = available_spawns(bot)
    if items:
        return items

    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        items = available_spawns(bot)
        if items:
            return items
        if not _connected(bot):
            return []
        time.sleep(poll)
    return []


def spawn_via_menu(
    bot,
    *,
    index: int | None = None,
    log=None,
    menu_timeout: float = MENU_TIMEOUT,
    timeout: float = SPAWN_TIMEOUT,
    stable_seconds: float = 2.0,
) -> dict:
    """Выбрать вокзал из меню сервера. Без меню возвращает status="no_menu"."""
    report = {"status": "no_menu", "position": None, "station": None, "choice": None}

    if not _connected(bot):
        report["status"] = "no_session"
        return report

    items = available_spawns(bot) or wait_spawn_menu(bot, menu_timeout)
    if not items:
        return report

    choice = menu_choice(items, bot) if index is None else int(index)
    report["choice"] = choice
    if log:
        log(f"[SPAWN] меню вокзалов {items}, выбираем {choice}")

    select_spawn(bot, choice)
    ok, pos = wait_spawned(bot, timeout, stable_seconds=stable_seconds)
    if ok:
        start_sync(bot)
        remember_station(pos)
        report.update(status="spawned", position=pos, station=station_name(pos))
        if log:
            log(
                f"[SPAWN] вокзал {report['station']} "
                f"X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}"
            )
        return report

    report["position"] = pos
    return report


# --- ожидание спавна --------------------------------------------------------

def wait_spawned(
    bot,
    timeout: float = SPAWN_TIMEOUT,
    *,
    stable_seconds: float = 2.0,
    poll: float = 0.25,
) -> tuple[bool, object]:
    """Ждать настоящий спавн и удержать позицию stable_seconds.

    Возвращает (успех, позиция). Кинематик в небе и зона преавторизации
    спавном не считаются.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    outside_since = None
    last = None
    while time.monotonic() < deadline:
        pos = _getpos(bot)
        last = pos
        if is_spawned_position(pos):
            if outside_since is None:
                outside_since = time.monotonic()
            elif time.monotonic() - outside_since >= stable_seconds:
                return True, pos
        else:
            outside_since = None
        if not _connected(bot):
            return False, last
        time.sleep(poll)
    return False, last


def force_spawn(
    bot,
    *,
    index: int | None = None,
    log=None,
    step_delay: float = STEP_DELAY,
    timeout: float = SPAWN_TIMEOUT,
    stable_seconds: float = 2.0,
    attempts: int = 2,
) -> dict:
    """Провести полный хендшейк спавна и вернуть отчёт."""
    wclass = DEFAULT_CLASS if index is None else int(index)
    report = {
        "status": "failed",
        "position": None,
        "class": wclass,
        "attempts": 0,
        "station": None,
    }

    if not _connected(bot):
        report["status"] = "no_session"
        return report

    pos = _getpos(bot)
    if is_spawned_position(pos):
        report.update(status="already_spawned", position=pos)
        report["station"] = station_name(pos)
        return report

    # Меню вокзалов — самый короткий путь. Если сервер его прислал,
    # RPC-хендшейк не нужен вовсе. Но меню от прошлой сессии не в счёт.
    if menu_received_recent(bot) and available_spawns(bot):
        menu_report = spawn_via_menu(
            bot,
            index=index,
            log=log,
            menu_timeout=0.0,
            timeout=timeout,
            stable_seconds=stable_seconds,
        )
        if menu_report.get("status") in ("spawned", "already_spawned"):
            menu_report["attempts"] = 1
            menu_report["class"] = wclass
            return menu_report
        report["position"] = menu_report.get("position")

    for attempt in range(1, max(1, attempts) + 1):
        if not _connected(bot):
            report["status"] = "no_session"
            return report
        report["attempts"] = attempt
        if log:
            log(f"[SPAWN] хендшейк, попытка {attempt} (class {wclass})")

        # 1. request_class -> RPC 372 SetSpawnInfo
        # Если уже висим в небе, 372 у сервера уже отдан — шаг пропускаем,
        # повторный request_class он игнорирует.
        if not in_sky(_getpos(bot)):
            request_class(bot, wclass, ordered=False, log=log)
            time.sleep(step_delay)

        # 2. spawn -> подтверждаем класс
        send_spawn(bot, ordered=False, log=log)
        time.sleep(max(1.0, step_delay / 2.0))

        # 3. request_class ordered -> RPC 448 SetPlayerPos на вокзал.
        # Сервер отвечает не с первого раза: пока он молчит или сбрасывает
        # в зону авторизации, повторяем, пока не сядем или не выйдет время.
        deadline = time.monotonic() + max(float(timeout), 1.0)
        pos = _getpos(bot)
        while _connected(bot):
            request_class(bot, wclass, ordered=True, log=log)
            window = min(6.0, max(1.5, deadline - time.monotonic()))
            ok, pos = wait_spawned(bot, window, stable_seconds=stable_seconds)
            if ok:
                start_sync(bot)
                remember_station(pos)
                name, dist = nearest_spawn_name(pos)
                label = name or station_name(pos)
                report.update(status="spawned", position=pos)
                report["station"] = label
                report["distance"] = round(dist, 1)
                if log:
                    log(
                        f"[SPAWN] вокзал {label} ({dist:.0f}м) "
                        f"X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}"
                    )
                # 4. закрепляем
                send_spawn(bot, ordered=True, log=log)
                return report
            if time.monotonic() >= deadline:
                break

        report["position"] = pos
        if log:
            where = "небо" if in_sky(pos) else "зона преавторизации" if in_auth_zone(pos) else "неизвестно"
            log(f"[SPAWN] попытка {attempt} не удалась: {where}")

    return report


# --- сторож спавна ----------------------------------------------------------

class SpawnWatchdog:
    """Следит, что бот в мире, и дотягивает его до вокзала."""

    OFFLINE_GRACE = 15.0

    # Сколько ждать уже запущенный спавн, прежде чем начинать свой.
    SPAWN_GRACE = 14.0

    # Сколько ждать меню вокзалов, прежде чем решать за сервер.
    MENU_WAIT = 6.0

    def __init__(self, bot, *, log=None, attempt_timeout=None, stable_seconds: float = 2.0,
                 grace: float | None = None, menu_wait: float | None = None):
        self.bot = bot
        self.log = log
        self.attempt_timeout = attempt_timeout or SPAWN_TIMEOUT
        self.stable_seconds = stable_seconds
        self.grace = self.SPAWN_GRACE if grace is None else max(0.0, float(grace))
        self.menu_wait = self.MENU_WAIT if menu_wait is None else max(0.0, float(menu_wait))
        self._stop = threading.Event()
        self._thread = None
        self.running = False
        self.report = None

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.running = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def _log(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def _run(self) -> None:
        offline_since = None
        try:
            while not self._stop.is_set():
                if not _connected(self.bot):
                    if offline_since is None:
                        offline_since = time.monotonic()
                    elif time.monotonic() - offline_since > self.OFFLINE_GRACE:
                        return
                    self._stop.wait(1.0)
                    continue
                offline_since = None

                pos = _getpos(self.bot)
                if is_spawned_position(pos):
                    self.report = {"status": "spawned", "position": pos}
                    self._mark_spawned(pos)
                    return

                # Спавн мог уже начаться: меню вокзалов отвечено, сервер
                # везёт нас кинематиком. Даём ему доехать, прежде чем
                # лезть с RPC-хендшейком — иначе он уведёт на другой вокзал.
                if menu_answer_recent(self.bot):
                    ok, pos = wait_spawned(
                        self.bot, self.grace, stable_seconds=self.stable_seconds
                    )
                    if ok:
                        self.report = {"status": "spawned", "position": pos}
                        self._mark_spawned(pos)
                        return

                if in_sky(pos) or in_auth_zone(pos) or pos is None:
                    # Меню вокзалов открыто, но ответ ещё не ушёл — ждём
                    # ответ, а не бьём хендшейком.
                    #
                    # Это была главная причина кика античита кодом 25.
                    # Ответ на меню отложен на 1.6-3.4с (человеческая
                    # реакция), а сторож видел «меню только что пришло» и
                    # решал, что ответит кто-то другой, после чего сразу
                    # звал force_spawn. В логе это видно построчно:
                    #     iid=50 меню вокзалов
                    #     >> request_class   — через 0.00с
                    #     >> spawn           — через 0.40с
                    # Сервер получает хендшейк при открытом меню и кикает.
                    if menu_received_recent(self.bot) and not menu_answer_recent(self.bot):
                        # Штатный обработчик отвечает с человеческой паузой.
                        # Даём ему время, и только если он так и не ответил —
                        # отвечаем сами. Но ни в каком случае не хендшейком:
                        # RPC при открытом меню — это и есть кик.
                        if not wait_menu_answered(self.bot, self.menu_wait):
                            select_spawn(self.bot, menu_choice(bot=self.bot))
                            wait_menu_answered(self.bot, 5.0)
                        ok, pos = wait_spawned(
                            self.bot, self.grace, stable_seconds=self.stable_seconds
                        )
                        if ok:
                            self.report = {"status": "spawned", "position": pos}
                            self._mark_spawned(pos)
                            return
                        self._stop.wait(0.5)
                        continue

                    # Меню ещё в пути: даём ему шанс, иначе хендшейк выберет
                    # вокзал за нас и уведёт с того, что выбрал бы игрок.
                    if not menu_received_recent(self.bot) and not menu_answer_recent(self.bot):
                        # Ожидание меню не должно съедать бюджет попытки:
                        # если сервер молчит, хендшейк нужен прямо сейчас.
                        budget = min(self.menu_wait, self.attempt_timeout / 2.0)
                        items = wait_spawn_menu(self.bot, budget)
                        if items:
                            select_spawn(self.bot, menu_choice(items, self.bot))
                            ok, pos = wait_spawned(
                                self.bot, self.grace, stable_seconds=self.stable_seconds
                            )
                            if ok:
                                self.report = {"status": "spawned", "position": pos}
                                self._mark_spawned(pos)
                                return

                    self.report = force_spawn(
                        self.bot,
                        log=self.log,
                        timeout=self.attempt_timeout,
                        stable_seconds=self.stable_seconds,
                    )
                    if self.report.get("status") in ("spawned", "already_spawned"):
                        self._mark_spawned(self.report.get("position"))
                        return

                self._stop.wait(2.0)
        finally:
            self.running = False

    def _mark_spawned(self, pos) -> None:
        start_sync(self.bot)
        marker = getattr(self.bot, "mark_spawned", None)
        if callable(marker):
            try:
                marker(pos)
            except Exception:
                pass


def ensure_spawn(bot, *, timeout: float = 60.0, log=None, on_success=None,
                 stable_seconds: float = 2.0) -> dict:
    """Дотянуть бота до вокзала в пределах общего таймаута."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    report = {"status": "timeout", "position": _getpos(bot)}

    while time.monotonic() < deadline:
        if not _connected(bot):
            report["status"] = "no_session"
            return report

        pos = _getpos(bot)
        if is_spawned_position(pos):
            report.update(status="already_spawned", position=pos)
            break

        remaining = deadline - time.monotonic()
        report = force_spawn(
            bot,
            log=log,
            timeout=min(SPAWN_TIMEOUT, max(4.0, remaining / 2.0)),
            stable_seconds=stable_seconds,
            attempts=1,
        )
        if report.get("status") in ("spawned", "already_spawned"):
            break
        time.sleep(0.5)

    if report.get("status") in ("spawned", "already_spawned") and on_success:
        try:
            on_success(report)
        except Exception:
            pass
    return report


# --- перемещение между вокзалами -------------------------------------------

def nearest_other_station(pos, exclude=None):
    """Ближайший вокзал, кроме текущего. Возвращает (имя, координаты, дистанция)."""
    if not pos or len(pos) < 3:
        return None, None, float("inf")
    if exclude is None:
        exclude = nearest_spawn_name(pos)[0]
    best, best_d = None, float("inf")
    for name, coords in SPAWNS.items():
        if name == exclude:
            continue
        d = math.dist(coords, (float(pos[0]), float(pos[1]), float(pos[2])))
        if d < best_d:
            best, best_d = name, d
    return best, SPAWNS.get(best) if best else None, best_d


def travel_to_station(bot, name, *, speed: float | None = None, log=None) -> dict:
    """Довезти бота до вокзала медленным delta-телепортом.

    Быстрее SAFE_SPEED сервер откатывает на доверенную точку, поэтому
    скорость по умолчанию — 8.5 м/с: позиция и velocity в пакете сходятся,
    и античит видит обычный бег.
    """
    from logic.delta_teleport import DeltaTeleport, SAFE_SPEED, step_for_speed

    coords = SPAWNS.get(name)
    if coords is None:
        return {"status": "unknown_station", "station": name, "position": _getpos(bot)}
    if not _connected(bot):
        return {"status": "no_session", "station": name, "position": _getpos(bot)}

    api = getattr(bot, "api", None)
    session = getattr(bot, "session", None)
    if api is None or session is None:
        return {"status": "no_api", "station": name, "position": _getpos(bot)}

    speed = SAFE_SPEED if speed is None else float(speed)
    interval = 0.02
    engine = DeltaTeleport(
        session.players.bot,
        api._delta_sync_sender,
        step=step_for_speed(speed, interval),
        interval=interval,
        velocity_mode="consistent",
        jitter=0.0,
        settle_packets=1,
        abort_check=lambda: not session.connected,
    )

    was_enabled = bool(getattr(session.sync, "enabled", False))
    if was_enabled:
        session.sync.stop()
    try:
        result = engine.run(coords)
    finally:
        if was_enabled and session.connected:
            session.sync.start()

    result["station"] = name
    if log:
        log(
            f"[TP] {name}: {result['status']} | {result['distance']:.0f}м за "
            f"{result['elapsed']:.0f}с | скорость {speed:.1f} м/с"
        )
    return result


def travel_to_nearest_station(bot, *, speed: float | None = None, log=None) -> dict:
    """Доехать до ближайшего ДРУГОГО вокзала — короткий маршрут."""
    pos = _getpos(bot)
    name, coords, dist = nearest_other_station(pos)
    if name is None:
        return {"status": "no_target", "position": pos}
    if log:
        log(f"[TP] ближайший вокзал «{name}» — {dist:.0f}м")
    return travel_to_station(bot, name, speed=speed, log=log)


# --- совместимость со старым API -------------------------------------------

def spawn_index_for(bot_id: int) -> int:
    """Класс спавна. Сервер отдаёт d_spawns_available = 1, класс всегда 0."""
    return DEFAULT_CLASS


def spawn_name(index: int):
    return None


PROBE_INDEXES: tuple[int, ...] = (DEFAULT_CLASS,)
SPAWN_INDEX: dict = {}


class SpawnMenuState:
    """Состояние меню вокзалов одного бота.

    Раньше это были переменные модуля — одни на весь процесс. При одном
    боте разницы нет. При многих это ломало вход, и именно отсюда рос кик
    античита кодом 25:

      * бот B, ещё не получив своего меню, видел «меню только что
        приходило» от бота A и лез отвечать на чужое меню;
      * если пункты у A и B совпадали, ключ уже отвеченного меню совпадал
        тоже, ответ у B не уходил вовсе, и он навсегда оставался в зоне
        авторизации;
      * reset_menu() при входе любого бота стирал меню всем остальным.

    Теперь состояние живёт на самом боте.
    """

    __slots__ = ("items", "received_at", "answered_at", "answered_key",
                 "pending_key", "lock")

    def __init__(self):
        self.items: list = []
        self.received_at = 0.0
        self.answered_at = 0.0
        self.answered_key = None
        # Кто-то прямо сейчас готовит ответ на это меню. Нужен, чтобы
        # сторож и обработчик меню не отправили ответ оба.
        self.pending_key = None
        self.lock = threading.Lock()


# Запасное состояние для вызовов без бота (тесты, разовые скрипты).
_DEFAULT_STATE = SpawnMenuState()


def menu_state(bot=None) -> "SpawnMenuState":
    """Состояние меню для конкретного бота."""
    if bot is None:
        return _DEFAULT_STATE
    state = getattr(bot, "spawn_menu_state", None)
    if isinstance(state, SpawnMenuState):
        return state
    state = SpawnMenuState()
    try:
        bot.spawn_menu_state = state
    except Exception:
        return _DEFAULT_STATE
    return state


def is_spawn_menu(payload) -> bool:
    """Сервер прислал список вокзалов, а не просто приглашение {"s": 1}."""
    return isinstance(payload, dict) and isinstance(payload.get("m"), list) and bool(payload["m"])


def remember_spawn_menu(payload, bot=None) -> list:
    """Запомнить список вокзалов от сервера и вернуть его."""
    if not is_spawn_menu(payload):
        return []
    items = [item for item in payload["m"] if isinstance(item, int)]
    if not items:
        return []
    state = menu_state(bot)
    state.items = items
    state.received_at = time.monotonic()
    return items


def menu_received_recent(bot=None, window: float = 40.0) -> bool:
    state = menu_state(bot)
    return bool(state.received_at) and (time.monotonic() - state.received_at) < window


def reset_menu(bot=None) -> None:
    """Сбросить состояние меню: новый сервер — новое меню.

    Только для этого бота. Раньше сбрасывалось глобально, и вход одного
    бота стирал меню всем остальным.
    """
    state = menu_state(bot)
    state.items = []
    state.received_at = 0.0
    state.answered_at = 0.0
    state.answered_key = None
    state.pending_key = None


def wait_menu_answered(bot, timeout: float = 8.0, *, poll: float = 0.15) -> bool:
    """Дождаться, пока ответ на меню вокзалов реально уйдёт.

    Нужен сторожу: ответ отложен на человеческую паузу, и всё это время
    нельзя бить хендшейком — сервер кикает за RPC при открытом меню.
    Возвращает True, если ответ ушёл или отвечать уже нечего.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if menu_answer_recent(bot):
            return True
        if not _connected(bot):
            return False
        time.sleep(poll)
    return menu_answer_recent(bot)


def menu_choice(items=None, bot=None) -> int:
    """Какой вокзал выбирать: первый из списка сервера.

    Индексы в меню — это id вокзалов, а не порядковые номера, поэтому берём
    именно первое значение, а не 1.
    """
    if items is None:
        items = menu_state(bot).items or []
    if items:
        return int(items[0])
    return 1


def build_menu_answer(items=None, bot=None) -> dict:
    """Ответ на меню: {"t": 1, "s": <id первого вокзала>}."""
    return {"t": 1, "s": menu_choice(items, bot)}


def available_spawns(bot=None) -> list:
    return list(menu_state(bot).items or [])


def build_spawn_payload(index: int, style: str | None = None) -> dict:
    return {"t": 1, "s": int(index)}


def menu_answer_recent(bot=None, window: float = 25.0) -> bool:
    """Отвечали ли мы на меню этого бота за последние window секунд.

    Нужно сторожу: после ответа сервер везёт нас кинематиком, и лезть с
    хендшейком нельзя.
    """
    state = menu_state(bot)
    return bool(state.answered_at) and (time.monotonic() - state.answered_at) < window


# Ключ отвеченного меню живёт в SpawnMenuState.answered_key: сервер
# присылает одно и то же меню несколько раз (и сторож, и хендшейк зовут
# select_spawn), и раньше ответ уходил по 15-18 раз за вход. Повторные
# ответы на одно меню — лишний шум на входе, а вход античит и проверяет.

# Пауза перед ответом на меню вокзала.
#
# Меню приходит на 15-й секунде входа, и мы отвечали в том же тике — за 0.0 с.
# Живой человек столько не думает. Античит это и ловит: в логах оба кика
# кодом 25 пришли через 0.1-0.3 с после мгновенного ответа, а вход, где меню
# не пришло вовсе, прошёл спокойно.
#
# Отвечаем не сразу, а через паузу, похожую на человеческую. Границы взяты
# с запасом: слишком короткая ничего не меняет, слишком длинная даёт шанс,
# что меню истечёт и спавн не состоится.
SPAWN_ANSWER_DELAY = (1.6, 3.4)


def spawn_answer_delay(rng=None) -> float:
    """Сколько подождать перед ответом на меню вокзала."""
    import random as _random

    low, high = SPAWN_ANSWER_DELAY
    return (rng or _random).uniform(low, high)


def select_spawn(bot, index: int | None = None, style: str | None = None,
                 *, delay: float | None = None) -> bool:
    """Ответить на меню вокзалов. Без индекса выбираем первый.

    На одно и то же меню отвечаем один раз. Повторный вызов с теми же
    пунктами ничего не отправляет и возвращает True.

    delay — пауза перед отправкой. По умолчанию человеческая (см.
    SPAWN_ANSWER_DELAY), 0 отключает её для тестов.
    """
    state = menu_state(bot)

    items = tuple(state.items or [])
    choice = menu_choice(bot=bot) if index is None else int(index)
    menu_key = (items, choice)

    # Захват ответа под замком.
    #
    # Пауза перед отправкой идёт ДО пакета, а дедуп раньше стоял до паузы.
    # Из-за этого сторож и обработчик меню оба проходили проверку, оба
    # спали свои 1.6-3.4с и оба отправляли SPAWN_SELECT. Два одинаковых
    # ответа на одно меню античит видит как дубль и кикает кодом 25.
    #
    # Теперь право на ответ занимается атомарно, ещё до паузы.
    with state.lock:
        if state.answered_key == menu_key:
            return True
        if state.pending_key == menu_key:
            # За это меню уже взялся другой поток — второй ответ не нужен.
            return True
        state.pending_key = menu_key

    session = getattr(bot, "session", None)
    if session is None or not _connected(bot):
        with state.lock:
            if state.pending_key == menu_key:
                state.pending_key = None
        return False

    try:
        pause = spawn_answer_delay() if delay is None else float(delay)
        if pause > 0:
            time.sleep(pause)

        payload = build_spawn_payload(choice)
        try:
            sent = bool(
                session.send_json(
                    InterfaceID.SPAWN_SELECT, payload,
                    packet_name="interface_spawn_select"
                )
            )
        except Exception:
            return False

        if sent:
            with state.lock:
                state.answered_at = time.monotonic()
                state.answered_key = menu_key
            try:
                bot.add_log(
                    f"[ВОКЗАЛ] ответ на меню через {pause:.1f}с: {payload} | "
                    f"пункты сервера: {list(items)}"
                )
            except Exception:
                pass
        return sent
    finally:
        # Освобождаем захват в любом случае: если отправка сорвалась,
        # следующий заход должен иметь право попробовать снова.
        with state.lock:
            if state.pending_key == menu_key:
                state.pending_key = None


def answer_spawn_later(bot, choice: int | None = None) -> bool:
    """Ответить на меню вокзала отложенно, в отдельном потоке.

    Пауза перед ответом обязательна: мгновенный ответ на меню — самый
    явный признак бота, и античит кикает именно за него. Но спать в
    обработчике пакетов нельзя, иначе на паузу встанет разбор всего
    входящего потока — это заметнее, чем сам ответ.

    Возвращает True, если поток запущен.
    """
    import threading

    if getattr(bot, "spawn_answer_busy", False):
        return False
    bot.spawn_answer_busy = True

    def _worker():
        try:
            if select_spawn(bot, choice):
                try:
                    bot.add_log(f"[SPAWN] отправлен выбор вокзала {choice}")
                except Exception:
                    pass
                watchdog = getattr(bot, "spawn_watchdog", None)
                if watchdog is None or not getattr(watchdog, "running", False):
                    starter = getattr(bot, "begin_spawn_watchdog", None)
                    if starter is not None:
                        starter()
        except Exception as exc:
            try:
                bot.add_log(f"[SPAWN] ошибка ответа: {exc!r}")
            except Exception:
                pass
        finally:
            bot.spawn_answer_busy = False

    threading.Thread(target=_worker, daemon=True).start()
    return True


def confirm_spawn(bot) -> None:
    send_spawn(bot, ordered=False)
