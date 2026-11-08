"""Маршрут сбора предметов на Нижегородске — координаты с живого клиента.

Снято из _diag/route2.pcap (31.10, 14:14:45–14:17:58). За сессию 15 нажатий
поднятия и 6 выдач: бутылка, две железные руды, шкура, два реагента.

Точки ниже — не «примерно там», а замер позиции бота в момент отправки
RPC 413 с телом 18, с точностью до 20 мс до выдачи. Расхождение с записью
не больше метра.

ВАЖНОЕ, ЧТО ВЫЯСНИЛОСЬ ПРИ РАЗБОРЕ

    Позиция, с которой началась сессия, (-1444.90, 2130.31, 9.62) — это
    НЕ вокзал. Оба Нижегородска из logic/spawn.SPAWNS лежат в тысяче
    метров отсюда:

        Нижегородск 1   (-2661.5, 2000.9)   ~1190 м
        Нижегородск 2   (-2152.8, 1558.3)    ~880 м

    То есть запись началась не со спавна: аккаунт вошёл на сохранённую
    позицию, меню вокзалов при этом пришло, но сервер персонажа не двигал.
    Поэтому площадка сбора совпадает с точкой входа, и «от спавна к месту»
    — это перелёт на 880–1190 м, а не прогулка.

    Спавн делаем ТОЛЬКО через меню (logic.spawn). RPC-хендшейк сервер
    наказывает киком античита кодом 25 — это уже проверено и зашито
    предохранителем ALLOW_RPC_SPAWN_HANDSHAKE = False.
"""

from __future__ import annotations

import math

# --- рабочие точки ----------------------------------------------------------

# Порядок — как шёл живой игрок. Замер в момент нажатия.
WORK_POINTS: list[tuple[float, float, float]] = [
    (-1476.70, 2120.38, 9.60),   # 14:16:31  бутылка
    (-1526.65, 2113.53, 9.62),   # 14:16:46  железная руда
    (-1575.63, 2107.02, 9.65),   # 14:17:01  железная руда
    (-1522.41, 2137.61, 15.75),  # 14:17:21  шкура животного
    (-1460.77, 2140.20, 12.60),  # 14:17:43  реагенты
]

# Центр площадки — по ней решаем, «мы на месте» или «нас занесло».
WORK_ANCHOR: tuple[float, float, float] = (-1500.0, 2128.0, 10.5)

# Дальше этого от центра считаем, что бот не на площадке и его надо везти.
WORK_RADIUS = 300.0

# Где заканчивается «мы в мире» и начинается «мы на вокзале или в небе».
STATION_RADIUS = 150.0


def distance(a, b) -> float:
    return math.dist(tuple(a), tuple(b))


def distance2d(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def on_work_ground(pos, *, anchor=WORK_ANCHOR, radius=WORK_RADIUS) -> bool:
    """Бот на площадке сбора."""
    if not pos or len(pos) < 3:
        return False
    return distance2d(pos, anchor) <= float(radius)


def needs_travel(pos, *, anchor=WORK_ANCHOR, radius=WORK_RADIUS) -> bool:
    """Бота надо везти на площадку."""
    return not on_work_ground(pos, anchor=anchor, radius=radius)


def nearest_work_point(pos, points=None):
    """Ближайшая рабочая точка. Возвращает (индекс, точка, расстояние)."""
    pts = WORK_POINTS if points is None else list(points)
    if not pos or len(pos) < 3 or not pts:
        return None, None, float("inf")
    best_i, best_p, best_d = None, None, float("inf")
    for i, p in enumerate(pts):
        d = distance(pos, p)
        if d < best_d:
            best_i, best_p, best_d = i, p, d
    return best_i, best_p, best_d


def next_point(pos, points=None, *, skip_within: float = 4.0):
    """Следующая точка по кругу после текущей позиции.

    Если бот стоит на точке (или почти на ней) — возвращаем следующую,
    иначе снова жали бы на том же месте.
    """
    pts = list(WORK_POINTS if points is None else points)
    if not pts:
        return None, None
    i, _, d = nearest_work_point(pos, pts)
    if i is None:
        return 0, pts[0]
    if d <= skip_within:
        nxt = (i + 1) % len(pts)
        return nxt, pts[nxt]
    return i, pts[i]


def round_length(points=None) -> float:
    """Длина полного круга по точкам, в метрах."""
    pts = list(WORK_POINTS if points is None else points)
    if len(pts) < 2:
        return 0.0
    total = 0.0
    for i in range(len(pts)):
        total += distance(pts[i], pts[(i + 1) % len(pts)])
    return total


def describe_route(points=None, *, anchor=WORK_ANCHOR) -> str:
    pts = list(WORK_POINTS if points is None else points)
    lines = [f"точек: {len(pts)}, круг {round_length(pts):.0f} м"]
    for i, p in enumerate(pts):
        lines.append(
            f"  #{i + 1}  ({p[0]:9.2f}, {p[1]:9.2f}, {p[2]:6.2f})"
            f"   {distance2d(p, anchor):5.0f} м от центра"
        )
    return "\n".join(lines)


def station_summary(pos, spawns=None) -> dict:
    """Что бот делает на вокзале и куда ему ехать.

    Считается без сети: берём позицию и таблицу вокзалов из logic.spawn.
    """
    out = {"station": None, "station_distance": float("inf"),
           "to_anchor": float("inf"), "needs_travel": True}
    if not pos or len(pos) < 3:
        return out
    try:
        from logic.spawn import SPAWNS, nearest_spawn_name
    except Exception:
        SPAWNS, nearest_spawn_name = {}, lambda p: (None, float("inf"))
    table = SPAWNS if spawns is None else spawns
    name, dist = nearest_spawn_name(pos)
    out["station"] = name
    out["station_distance"] = dist
    out["to_anchor"] = distance(pos, WORK_ANCHOR)
    out["needs_travel"] = needs_travel(pos)
    if name is None and dist > STATION_RADIUS:
        # Вокзал не из таблицы — подписываем координатами, чтобы в логе
        # было видно, куда именно нас поставило.
        out["station"] = f"неизвестный ({float(pos[0]):.0f}, {float(pos[1]):.0f})"
    return out
