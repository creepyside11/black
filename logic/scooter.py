"""Аренда скутера и мгновенный уход с парковки.

Механика, снятая с живого сервера (IZHEVSK):

    пикап аренды (model 19131)
      -> ДИАЛОГ style=5 "Управление транспортом"
           строка 0: "1. Скутер [------] Арендовать"
      -> ответ list_item=0, response=1
      -> ДИАЛОГ style=0 "Аренда": "Арендовать Скутер за 100 рублей?"
      -> ответ response=1 ("Да")
      -> RPC 353 PUT_PLAYER_IN_VEHICLE   (сели)
      -> iid=39 "Покиньте территорию парковки. Времени осталось 30 сек."

Главное, что стоило 100 рублей и скутера: между ответом "Да" и телепортом
нельзя вставлять паузу. Сервер запускает таймер парковки в тот же тик, что
сажает игрока, и пока бот "проверяет состояние" — окно догорает, деньги
списываются, транспорт снимается. Поэтому здесь ответ и уход идут подряд,
без единого sleep между ними.

Езда (а не телепорт) идёт через штатный CoordMovementController: он жмёт
реальные клавиши и шлёт синхронизацию, сервер видит обычного водителя.
"""
from __future__ import annotations

import math
import time

# Заголовки диалогов (цветовые коды сервер подмешивает, ищем по подстроке).
MENU_TITLE = "Управление транспортом"
CONFIRM_TITLE = "Аренда"
CONFIRM_WORD = "рубл"

# Пикап аренды: model 19131 — стойка проката.
RENTAL_MODEL = 19131

# Сколько ждать каждое окно. Меньше секунды сервер не успевает ответить.
MENU_TIMEOUT = 8.0
CONFIRM_TIMEOUT = 6.0

# Радиус поиска пикапа аренды вокруг точки.
PICKUP_RADIUS = 8.0

# Клавиши вождения: как часто шагает контроллер.
DRIVE_INTERVAL = 0.05


def exit_vehicle(bot):
    """Сойти с транспорта. Единственный рабочий путь — setbotvehicle(0).

    Сервер снимает игрока с сиденья по RPC exit_vehicle, и API отправляет
    его только из setbotvehicle(0): там же меняется sync_state на onfoot.
    Прямого метода выхода в BotAPI нет, а руками собранный пакет оставляет
    бота «в машине» для планировщика синхронизации.
    """
    player = bot.session.players.bot
    try:
        vid = int(player.get_current_vehicle_id())
    except Exception:
        vid = 0
    if vid <= 0:
        return {"exited": False, "reason": "уже пешком", "vehicle_id": 0}

    ok = bool(bot.api.setbotvehicle(0))
    return {
        "exited": ok,
        "previous_vehicle_id": vid,
        "vehicle_id": 0 if ok else vid,
        "position": _pos(bot),
    }


def _pos(bot):
    try:
        p = bot.api.getbotposition()
        return (float(p[0]), float(p[1]), float(p[2]))
    except Exception:
        return None


def _title(entry):
    return str((entry or {}).get("title", ""))


def _plain(text):
    """Убрать SA-MP цвета, чтобы сравнивать заголовки без {E0584B}."""
    out, i = [], 0
    text = str(text or "")
    while i < len(text):
        if text[i] == "{" and i + 7 < len(text) and text[i + 7] == "}":
            i += 8
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def find_rental_pickup(bot, point, radius=PICKUP_RADIUS):
    """Ближайший пикап аренды к точке. Возвращает (pickup_id, distance)."""
    try:
        picks = bot.session.stream.get_all_pickups() or {}
    except Exception:
        return None
    best = None
    for pid, pk in picks.items():
        try:
            if int(pk.get("model_id") or 0) != RENTAL_MODEL:
                continue
            d = math.dist((float(pk["x"]), float(pk["y"]), float(pk["z"])), point)
        except Exception:
            continue
        if d <= radius and (best is None or d < best[1]):
            best = (int(pid), d)
    return best


def poke_pickup(bot, pickup_id):
    """RPC подбора пикапа — только так сервер открывает меню."""
    from samp.packets import PacketBuilder
    sent = False
    for name in ("picked_up_pickup", "picked_up_interact"):
        try:
            bot.session._sender.send(
                PacketBuilder.rpc_build(name, int(pickup_id)),
                rpc_name=name,
            )
            sent = True
        except Exception:
            pass
    return sent


def _wait_dialog(bot, title_needle, deadline, dialogs):
    """Ждём в списке dialogs окно с нужным заголовком."""
    while time.time() < deadline:
        for entry in reversed(list(dialogs or [])):
            if title_needle in _plain(_title(entry)):
                return entry
        if not bot.session.connected:
            return None
        time.sleep(0.05)
    return None


def _pick_row(entry, needle):
    """Индекс строки меню, где встречается needle. Заголовок таблицы — не строка."""
    from logic.farm import item_text, parse_list_dialog
    items = entry.get("items")
    if not items:
        items = parse_list_dialog(entry.get("info", ""), entry.get("style", 5))
    for index, item in enumerate(items):
        if needle.lower() in item_text(item).lower():
            return index
    return None


def rent_scooter(
    bot,
    escape_point,
    *,
    pickup_point=None,
    teleport=None,
    dialogs=None,
    log=print,
    menu_timeout=MENU_TIMEOUT,
    confirm_timeout=CONFIRM_TIMEOUT,
):
    """Взять скутер и в тот же тик уйти на escape_point.

    pickup_point — где искать стойку проката (по умолчанию текущая позиция).
    teleport     — функция tp(bot, (x, y, z)); без неё скутер не спасти.
    dialogs      — живой список диалогов агента (последние снизу).

    Возвращает dict: rented, escaped, vehicle_id, reason.
    """
    result = {"rented": False, "escaped": False, "vehicle_id": None, "reason": ""}

    if teleport is None:
        result["reason"] = "нет функции телепорта — уходить с парковки нечем"
        return result

    here = pickup_point or _pos(bot)
    if here is None:
        result["reason"] = "позиция неизвестна"
        return result

    found = find_rental_pickup(bot, here)
    if found is None:
        result["reason"] = f"пикап аренды не найден в {PICKUP_RADIUS:.0f}м от {here}"
        return result
    pickup_id, dist = found
    log(f"[scooter] стойка проката #{pickup_id} в {dist:.1f}м")

    # 1. Открываем меню транспорта.
    deadline = time.time() + menu_timeout
    menu = None
    while time.time() < deadline and menu is None:
        poke_pickup(bot, pickup_id)
        menu = _wait_dialog(bot, MENU_TITLE, time.time() + 2.0, dialogs)
    if menu is None:
        result["reason"] = "меню аренды не открылось"
        return result

    row = _pick_row(menu, "скутер")
    if row is None:
        result["reason"] = f"в меню нет скутера: {menu.get('info', '')[:120]}"
        return result

    # 2. Выбираем скутер.
    if not bot.session.send_dialog_response(menu.get("dialog_id", 0), 1, row, ""):
        result["reason"] = "ответ по меню не ушёл"
        return result
    log(f"[scooter] выбрал строку {row} (скутер)")

    # 3. Подтверждаем аренду — и сразу уходим, без паузы.
    confirm = _wait_dialog(bot, CONFIRM_TITLE, time.time() + confirm_timeout, dialogs)
    if confirm is None or CONFIRM_WORD not in _plain(confirm.get("info", "")):
        result["reason"] = "окно подтверждения аренды не пришло"
        return result

    answer_ok = bot.session.send_dialog_response(confirm.get("dialog_id", 0), 1, 0, "")
    if not answer_ok:
        result["reason"] = "ответ подтверждения не ушёл"
        return result
    result["rented"] = True
    log("[scooter] аренда подтверждена — ухожу с парковки немедленно")

    # Вот он, критичный участок: никаких проверок и sleep до телепорта.
    teleport(bot, tuple(escape_point))

    try:
        result["vehicle_id"] = bot.session.players.bot.get_current_vehicle_id()
    except Exception:
        pass

    final = _pos(bot)
    if final is not None:
        left = math.dist(final, tuple(escape_point))
        result["escaped"] = left <= 5.0
        log(f"[scooter] стою {final[0]:.1f},{final[1]:.1f},{final[2]:.1f} "
            f"| до цели {left:.1f}м | vehicle={result['vehicle_id']}")
    return result


# Скорость доворота корпуса, % от мгновенного. 45% ≈ 160°/с: разворот на
# 180° занимает около секунды, как у живой техники. При 100% корпус
# телепортировался в новый угол — это и читалось как резкий поворот.
TURN_PERCENT = 45

# Прямая езда: без объезда, лицом по курсу, клавиши зажаты.
STRAIGHT = {
    "avoid_obstacles": False,            # вшитый объезд сдвигал вбок на 0.35м/шаг
    "movement_direction_control": (True, True, 0, TURN_PERCENT),  # по курсу, плавно
    "movement_keys_mode": "throttle",    # жать клавиши, как живой игрок
    "coord_fluctuation_max": 0.0,        # ни малейшего дрожания
    "accel_time": 1.2,
    "brake_time": 0.6,
    "turn_radius": 3.0,   # метров: радиус полного руля, как у скутера
    "max_steer": 35.0,    # градусов: предел поворота руля
}

# Шаг за тик. Скорость скутера ~15 м/с при 20 тиках/с.
SCOOTER_STEP = 0.40

# Частота пакетов при езде. В ECO-режиме бот синхронизируется раз в 0.8-1.2с:
# для стоячего это нормально, но машина тогда прыгает на 3.75м за пакет —
# со стороны это и есть "рывки". Живой клиент шлёт ~20 раз в секунду.
DRIVE_SYNC_RATE = 0.05

# Сетевой цикл на время езды. Ниже 20 Гц пакеты идут слишком редко,
# чтобы движение читалось как плавное.
DRIVE_FPS = 30.0


def _fast_sync(bot, rate=DRIVE_SYNC_RATE, fps=DRIVE_FPS):
    """Переключить синхронизацию и сетевой цикл на водительскую частоту.

    Синхронизация тикает только из сетевого цикла, поэтому частота пакетов
    не может быть выше fps. В ECO-режиме fps=6, и машина прыгает по 2.5м
    за пакет — со стороны это рывки. На время езды поднимаем и то, и другое.

    Возвращает функцию отката к прежним значениям.
    """
    sync = getattr(bot.session, "sync", None)
    session = getattr(bot, "session", None)
    if sync is None or session is None:
        return lambda: None

    saved = {
        "rate_min": float(getattr(sync, "rate_vehicle_min", 0.0)),
        "rate_max": float(getattr(sync, "rate_vehicle_max", 0.0)),
        "p_min": float(getattr(sync, "rate_player_min", 0.0)),
        "p_max": float(getattr(sync, "rate_player_max", 0.0)),
        "next": float(getattr(sync, "_next_player_sync_time", 0.0)),
        "fps": float(getattr(session, "fps", 6.0)),
        "tick": float(getattr(session, "_network_tick_interval", 0.1667)),
    }

    session.fps = float(fps)
    session._network_tick_interval = 1.0 / float(fps)
    # Пешком MoveToCoord берёт интервал из rate_player_*, в машине — из
    # rate_vehicle_*. В ECO-режиме оба стоят около секунды, и шаг выходит
    # в 30 раз меньше нужного: бот топчется на месте. Поднимаем оба.
    sync.rate_vehicle_min = float(rate)
    sync.rate_vehicle_max = float(rate)
    sync.rate_player_min = float(rate)
    sync.rate_player_max = float(rate)
    sync._next_player_sync_time = 0.0

    def restore():
        session.fps = saved["fps"]
        session._network_tick_interval = saved["tick"]
        sync.rate_vehicle_min = saved["rate_min"]
        sync.rate_vehicle_max = saved["rate_max"]
        sync.rate_player_min = saved["p_min"]
        sync.rate_player_max = saved["p_max"]
        # Возвращать старое время следующего синка нельзя: за время
        # маршрута оно уходит в прошлое и синк замирает на секунды.
        # Ноль заставляет отправить пакет на ближайшем тике.
        sync._next_player_sync_time = 0.0

    return restore


# Частота собственного потока движения. Сетевой цикл на телефоне выдаёт
# около 10 тиков/с, и шаг в 1.5м читается как рывки. Двигаем и шлём сами.
DRIVE_HZ = 30.0
SCOOTER_SPEED = 15.0          # м/с

def _drive_loop(bot, done):
    """Крутить движение и отправку синхронизации с постоянной частотой."""
    from samp.packets import PacketBuilder

    player = bot.session.players.bot
    sender = bot.session._sender
    period = 1.0 / DRIVE_HZ
    next_at = time.monotonic()

    while not done.is_set():
        if not bot.session.connected:
            break
        try:
            player.advance_movement_step()
            sender.send(
                PacketBuilder.vehicle_sync(player),
                packet_name="vehicle_sync",
            )
        except Exception:
            break
        next_at += period
        delay = next_at - time.monotonic()
        if delay > 0.0:
            time.sleep(delay)
        else:
            next_at = time.monotonic()


def _walk_loop(bot, done, hz=None):
    """Идти ногами самому: шаг и player_sync с постоянной частотой.

    Сетевой цикл на телефоне отдаёт единицы тиков в секунду, и пешком
    это выглядит как исчезновение и появление в новой точке: сервер
    видит телепорт без скорости и без клавиш. Свой поток двигает бота
    ровно на 30 Гц и каждый тик отправляет пакет, поэтому в игре видна
    анимация бега, а не прыжок.
    """
    from samp.packets import PacketBuilder

    player = bot.session.players.bot
    sender = bot.session._sender
    period = 1.0 / float(hz or WALK_SYNC_HZ)
    next_at = time.monotonic()

    # Первый пакет отправляем сразу: если отрезок окажется мгновенным,
    # поток всё равно успеет сообщить серверу позицию и клавиши.
    first = True
    while first or not done.is_set():
        first = False
        if not bot.session.connected:
            break
        try:
            player.advance_movement_step()
            sender.send(
                PacketBuilder.player_sync(player),
                packet_name="player_sync",
            )
        except Exception:
            break
        next_at += period
        delay = next_at - time.monotonic()
        if delay > 0.0:
            time.sleep(delay)
        else:
            next_at = time.monotonic()


def _apply_settings(bot, settings):
    """Положить настройки в конфиг контроллера. Возвращает откат."""
    player = bot.session.players.bot
    controller = getattr(player, "coord_movement", None)
    if controller is None:
        return lambda: None
    config = controller.config
    saved = {k: config.get(k) for k in settings}
    config.update(settings)

    def restore():
        for key, value in saved.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value

    return restore


def _open_route(bot, settings, log=print, hz=None, walk=False):
    """Поднять режим движения: настройки, свой поток, синхронизация."""
    import threading

    from samp.packets import PacketBuilder  # noqa: F401  (проверка импорта)

    player = bot.session.players.bot
    sync = getattr(bot.session, "sync", None)

    restore_settings = _apply_settings(bot, settings)
    restore_sync = _fast_sync(bot) if hz is None else _fast_sync(
        bot, rate=1.0 / float(hz), fps=float(hz)
    )
    was_enabled = bool(getattr(sync, "enabled", False))
    if was_enabled:
        sync.stop()                       # иначе пакеты пойдут дважды

    done = threading.Event()
    if walk:
        driver = threading.Thread(
            target=_walk_loop, args=(bot, done, hz), daemon=True,
            name="walk-loop",
        )
    else:
        driver = threading.Thread(
            target=_drive_loop, args=(bot, done), daemon=True,
            name="scooter-drive",
        )
    driver.start()

    def close():
        done.set()
        driver.join(timeout=1.0)
        if was_enabled and bot.session.connected:
            sync.start()
        restore_sync()
        restore_settings()

    return close


def _run_leg(
    bot, target, *, timeout, completion_distance,
    mode="vehicle", step=SCOOTER_STEP, keys="throttle", turn=TURN_PERCENT,
):
    """Один отрезок: поставить цель и ждать, пока контроллер её отработает."""
    try:
        movement_id = bot.api.MoveToCoord(
            float(target[0]), float(target[1]), float(target[2]),
            mode=mode,
            coord_delay=step,
            completion_distance=completion_distance,
            movement_direction_control=(True, True, 0, turn),
            movement_keys_mode=keys,
        )
    except Exception as exc:
        return {"status": "error", "reason": repr(exc)}

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not bot.session.connected:
            return {"status": "disconnected", "movement_id": movement_id}
        try:
            active = bot.session.players.bot.coord_movement.get_active(movement_id)
        except Exception:
            active = None
        if active is None:
            break
        time.sleep(0.05)
    else:
        try:
            bot.api.StopMoveToCoord(movement_id, reason="timeout")
        except Exception:
            pass
        return {"status": "timeout", "movement_id": movement_id}

    final = _pos(bot)
    left = math.dist(final, tuple(target)) if final else float("inf")
    return {
        "status": "arrived" if left <= completion_distance + 2.0 else "stopped",
        "movement_id": movement_id,
        "final_position": final,
        "left": left,
    }


def drive_to(
    bot,
    target,
    *,
    timeout=180.0,
    completion_distance=3.0,
    step=SCOOTER_STEP,
    log=print,
    **overrides,
):
    """Ехать к одной точке: клавиши, синхронизация 30 Гц, плавный поворот."""
    start = _pos(bot)
    if start is None:
        return {"status": "no_position"}

    settings = dict(STRAIGHT)
    settings.update(overrides)
    settings["coord_delay"] = float(step)
    settings["completion_distance"] = float(completion_distance)

    log(f"[drive] едем {math.dist(start, tuple(target)):.0f}м до "
        f"({target[0]:.1f},{target[1]:.1f},{target[2]:.1f}) прямой")

    close = _open_route(bot, settings, log)
    try:
        result = _run_leg(
            bot, target, timeout=timeout,
            completion_distance=completion_distance,
        )
    finally:
        close()

    if result.get("final_position"):
        final = result["final_position"]
        log(f"[drive] {result['status']} | осталось {result.get('left', 0):.1f}м "
            f"| стою {final[0]:.1f},{final[1]:.1f},{final[2]:.1f}")
    return result


# Пешком: человек разворачивается вокруг себя, но не мгновенно.
WALK_STEP = 0.14          # метров за тик при 30 Гц ≈ 4.2 м/с (бег)
WALK_SPEED = 4.2          # м/с: с этой скоростью идёт пешеход
WALK_TURN_PERCENT = 70
WALK_RADIUS = 0.35        # метров: радиус разворота пешехода
WALK_SYNC_HZ = 30.0


PAUSE_HOLD = 0.4          # секунд тишины: сервер не видит промежуточных точек
PAUSE_MAX_STEP = 45.0     # метров за один прыжок: дальше сервер откатывает


def _warp_once(bot, target, hold):
    """Один прыжок в паузе синка."""
    player = bot.session.players.bot
    sync = getattr(bot.session, "sync", None)
    was_enabled = bool(getattr(sync, "enabled", False))

    if was_enabled:
        sync.stop()
    try:
        time.sleep(max(0.0, float(hold)))
        player._set_position_internal(
            target[0], target[1], target[2], update_velocity=False,
        )
        player.clear_position_target()
        player.reset_velocity()
        time.sleep(max(0.0, float(hold)))
    finally:
        if was_enabled and bot.session.connected:
            sync.start()


def pause_warp(
    bot, target, *, hold=PAUSE_HOLD, max_step=PAUSE_MAX_STEP, log=print,
):
    """Переместиться в паузе синхронизации, при необходимости кусками.

    Пока синк остановлен, сервер не получает ни одного пакета с позицией.
    Внутри паузы бот переносится в точку, а с первым пакетом после старта
    сервер видит его уже там.

    Один прыжок сервер принимает только на короткую дистанцию: слишком
    большой скачок он откатывает назад. Поэтому далёкая цель проходится
    серией коротких прыжков, каждый — со своей паузой.
    """
    target = tuple(float(v) for v in target)
    start = _pos(bot)
    if start is None:
        return {"status": "no_position"}

    max_step = max(1.0, float(max_step))
    total = math.dist(start, target)
    steps = max(1, int(math.ceil(total / max_step)))
    points = []
    for index in range(1, steps + 1):
        ratio = index / steps
        points.append((
            start[0] + (target[0] - start[0]) * ratio,
            start[1] + (target[1] - start[1]) * ratio,
            start[2] + (target[2] - start[2]) * ratio,
        ))

    reached = 0
    for index, point in enumerate(points, 1):
        _warp_once(bot, point, hold)
        current = _pos(bot)
        if current is None:
            break
        if math.dist(current, point) > max(2.0, max_step * 0.5):
            log(f"[warp] кусок {index}/{steps} откатился — дальше не иду")
            break
        reached = index

    final = _pos(bot)
    left = math.dist(final, target) if final else float("inf")
    log(f"[warp] пауза: ({start[0]:.1f},{start[1]:.1f}) -> "
        f"({final[0]:.1f},{final[1]:.1f}) | кусков {reached}/{steps} | "
        f"осталось {left:.2f}м")
    return {
        "status": "arrived" if left <= 2.0 else "stopped",
        "start_position": start,
        "final_position": final,
        "left": left,
        "steps": steps,
        "steps_done": reached,
    }


def walk_to(
    bot,
    target,
    *,
    timeout=240.0,
    completion_distance=1.2,
    step=WALK_STEP,
    log=print,
):
    """Дойти до точки пешком: клавиши, плавные повороты, без телепорта."""
    start = _pos(bot)
    if start is None:
        return {"status": "no_position"}

    settings = dict(STRAIGHT)
    settings["walk_turn_radius"] = WALK_RADIUS
    settings["steer_model"] = False       # пешком идём к точке, а не рулём
    settings["avoid_obstacles"] = False

    log(f"[walk] идём {math.dist(start, tuple(target)):.0f}м до "
        f"({target[0]:.1f},{target[1]:.1f},{target[2]:.1f})")

    close = _open_route(bot, settings, log, hz=WALK_SYNC_HZ, walk=True)
    try:
        result = _run_leg(
            bot, target, timeout=timeout,
            completion_distance=completion_distance,
            mode="walk", step=step, keys="throttle", turn=WALK_TURN_PERCENT,
        )
    finally:
        close()

    final = result.get("final_position")
    if final:
        log(f"[walk] {result['status']} | осталось "
            f"{result.get('left', 0):.1f}м | стою "
            f"{final[0]:.1f},{final[1]:.1f},{final[2]:.1f}")
    return result


def walk_route(
    bot,
    points,
    *,
    timeout=240.0,
    completion_distance=1.2,
    step=WALK_STEP,
    log=print,
):
    """Пройти несколько точек пешком подряд, не останавливаясь."""
    points = [tuple(float(v) for v in pt) for pt in points]
    if not points:
        return {"status": "empty_route", "legs": []}

    start = _pos(bot)
    if start is None:
        return {"status": "no_position", "legs": []}

    settings = dict(STRAIGHT)
    settings["walk_turn_radius"] = WALK_RADIUS
    settings["steer_model"] = False
    settings["avoid_obstacles"] = False

    total = sum(math.dist(a, b) for a, b in zip([start] + points, points))
    log(f"[walk] пеший маршрут из {len(points)} точек, всего {total:.0f}м")

    close = _open_route(bot, settings, log, hz=WALK_SYNC_HZ, walk=True)
    legs = []
    try:
        for index, point in enumerate(points, 1):
            log(f"[walk] отрезок {index}/{len(points)} -> "
                f"({point[0]:.1f},{point[1]:.1f},{point[2]:.1f})")
            result = _run_leg(
                bot, point, timeout=timeout,
                completion_distance=completion_distance,
                mode="walk", step=step, keys="throttle", turn=WALK_TURN_PERCENT,
            )
            result["target"] = point
            legs.append(result)
            log(f"[walk] отрезок {index}: {result['status']} | осталось "
                f"{result.get('left', 0):.1f}м")
            if result["status"] in ("disconnected", "error", "timeout"):
                break
    finally:
        close()

    ok = all(leg["status"] == "arrived" for leg in legs)
    return {"status": "arrived" if ok else "incomplete", "legs": legs}


def drive_route(
    bot,
    points,
    *,
    timeout=180.0,
    completion_distance=3.0,
    step=SCOOTER_STEP,
    log=print,
    **overrides,
):
    """Проехать несколько точек подряд, не останавливаясь между ними.

    Ездовой режим поднимается один раз на весь маршрут: между отрезками
    нет ни паузы, ни перезапуска синхронизации — машина идёт непрерывно.
    """
    points = [tuple(float(v) for v in pt) for pt in points]
    if not points:
        return {"status": "empty_route", "legs": []}

    start = _pos(bot)
    if start is None:
        return {"status": "no_position", "legs": []}

    settings = dict(STRAIGHT)
    settings.update(overrides)
    settings["coord_delay"] = float(step)
    settings["completion_distance"] = float(completion_distance)

    total = sum(math.dist(a, b) for a, b in zip([start] + points, points))
    log(f"[drive] маршрут из {len(points)} точек, всего {total:.0f}м")

    close = _open_route(bot, settings, log)
    legs = []
    try:
        for index, point in enumerate(points, 1):
            log(f"[drive] отрезок {index}/{len(points)} -> "
                f"({point[0]:.1f},{point[1]:.1f},{point[2]:.1f})")
            result = _run_leg(
                bot, point, timeout=timeout,
                completion_distance=completion_distance,
            )
            result["target"] = point
            legs.append(result)
            log(f"[drive] отрезок {index}: {result['status']} "
                f"| осталось {result.get('left', 0):.1f}м")
            if result["status"] in ("disconnected", "error", "timeout"):
                break
    finally:
        close()

    ok = all(leg["status"] == "arrived" for leg in legs)
    return {"status": "arrived" if ok else "incomplete", "legs": legs}
