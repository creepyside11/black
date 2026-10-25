"""Вывод бота в общий мир после входа.

Проверенная последовательность (REAL_WORLD_SPAWN.md):

1. Дождаться, что бот действительно в игре: сервер выдал позицию.
2. Если сервер уже разрешил спавн, но клиент не отправил Spawn —
   отправить ОДИН раз. Именно пропущенный Spawn давал пустой стрим:
   позиция есть, статус «заспавнен», а игроков и машин вокруг ноль.
3. Дождаться в стриме других игроков. Это и есть доказательство общего
   мира: интерьер 0 и глобальный онлайн им не являются.
4. Если стрим пуст — перейти на известный вокзал и проверить снова.

Чего здесь намеренно НЕТ: повторных подтверждений Spawn в цикле и
включения ALLOW_RPC_SPAWN_HANDSHAKE. Раньше именно это выкидывало
сессию кодом 25. Spawn уходит не больше одного раза за вход.
"""

from __future__ import annotations

import threading
import time

# Сколько ждать других игроков после входа.
PROOF_TIMEOUT = 40.0
# Сколько ждать саму игру (соединение и позицию).
ENTER_TIMEOUT = 90.0
# Сколько ждать разрешения на спавн. Своё окно, а не остаток от входа:
# на медленном прокси вход съедает почти весь enter_timeout, и на спавн
# не остаётся времени — бот застревал на «мир не подтверждён».
SPAWN_WAIT = 45.0
POLL = 1.0


def _log(log, message: str) -> None:
    if log:
        try:
            log(message)
        except Exception:
            pass


def position(bot):
    try:
        pos = bot.api.getbotposition() if bot.api else None
    except Exception:
        pos = None
    if not pos or len(pos) < 3:
        return None
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    if x == 0.0 and y == 0.0:
        return None
    return (x, y, z)


def streamed_players(bot) -> list[dict]:
    """Игроки в стриме, кроме себя и NPC.

    Себя исключаем по player_id: собственный игрок тоже лежит в стриме,
    и без этой проверки бот «подтверждал» бы мир самим собой.
    """
    session = getattr(bot, "session", None)
    if session is None:
        return []
    players = getattr(session, "players", None)
    if players is None:
        return []
    try:
        stream = players.get_streamed_players()
    except Exception:
        return []
    try:
        own_id = getattr(getattr(players, "bot", None), "player_id", None)
    except Exception:
        own_id = None

    found = []
    for pid, player in (stream or {}).items():
        if own_id is not None and pid == own_id:
            continue
        # Только явный флаг. В стриме это поле приходило байтом 0xFF у
        # живых игроков, и проверка на истинность выкидывала всех: бот
        # считал мир пустым и «подтверждался» по чату.
        if getattr(player, "is_npc", False) in (True, 1):
            continue
        name = getattr(player, "name", "") or ""
        if not name:
            continue
        found.append({
            "id": pid,
            "name": name,
            "position": [
                round(float(getattr(player, "x", 0.0)), 1),
                round(float(getattr(player, "y", 0.0)), 1),
                round(float(getattr(player, "z", 0.0)), 1),
            ],
        })
    return found


def probe(bot) -> dict:
    """Снимок: где бот, кто рядом, что говорит сервер о спавне."""
    session = getattr(bot, "session", None)
    server_info = getattr(session, "server_info", None)
    pos = position(bot)
    players = streamed_players(bot)
    return {
        "connected": bool(getattr(session, "connected", False)),
        "position": list(pos) if pos else None,
        "players": players,
        "player_count": len(players),
        "spawn_response": bool(getattr(server_info, "spawn_response", False)),
        "spawn_sent": bool(getattr(session, "_rpc_spawn_queued", False)),
        "vehicles": len(getattr(getattr(session, "stream", None), "vehicles", {}) or {}),
        "pickups": len(getattr(getattr(session, "stream", None), "pickups", {}) or {}),
    }


def finish_spawn(bot) -> str:
    """Отправить единственное финальное Spawn после ответа меню.

    Важно: это не RPC-хендшейк с request_class. Меню уже выбрало вокзал и
    сервер выставил настоящую позицию. Если после этого сервер выставил
    spawn_response, ему нужен один финальный Spawn, иначе клиент получает
    чат и локальную позицию, но не попадает в player stream других игроков.
    """
    session = getattr(bot, "session", None)
    if session is None or not getattr(session, "connected", False):
        return "offline"
    info = getattr(session, "server_info", None)
    if info is None or not getattr(info, "spawn_response", False):
        return "not_allowed"
    if getattr(session, "_rpc_spawn_queued", False):
        return "already"
    try:
        session.send_rpc_spawn()
        session._rpc_spawn_queued = True
    except Exception as exc:
        return f"failed: {exc}"
    return "sent"


def wait_players(bot, timeout: float, *, log=None, quiet: bool = False) -> list[dict]:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not getattr(bot.session, "connected", False):
            return []
        players = streamed_players(bot)
        if players:
            return players
        time.sleep(POLL)
    if not quiet:
        _log(log, f"[МИР] {timeout:.0f}с без игроков в стриме")
    return []


def _nearest_station(pos):
    try:
        from logic.spawn import SPAWNS
    except Exception:
        return None, None
    best, best_d = None, None
    for name, coords in SPAWNS.items():
        dist = ((pos[0] - coords[0]) ** 2 + (pos[1] - coords[1]) ** 2) ** 0.5
        if best_d is None or dist < best_d:
            best, best_d = (name, coords), dist
    return (best[0], best[1]) if best else (None, None)


def warp_to_station(bot, *, log=None) -> dict:
    """Перейти на ближайший вокзал в паузе синка."""
    pos = position(bot)
    if pos is None:
        return {"ok": False, "error": "нет позиции"}
    name, coords = _nearest_station(pos)
    if not coords:
        return {"ok": False, "error": "нет таблицы вокзалов"}
    try:
        from logic.scooter import pause_warp
    except Exception as exc:
        return {"ok": False, "error": f"нет pause_warp: {exc}"}
    _log(log, f"[МИР] стрим пуст — перехожу на «{name}»")
    result = pause_warp(bot, coords, log=lambda _m: None)
    return {"ok": result.get("status") == "arrived", "station": name, **result}


def _wait_spawn_allowed(bot, deadline: float, *, log=None) -> str:
    """Дождаться, пока сервер разрешит спавн, и отправить его один раз.

    Возвращает то же, что finish_spawn: sent / already / not_allowed /
    offline / failed. Ждём до deadline, но не бесконечно: если сервер так
    и не выставил spawn_response (например, бот уже был в мире), просто
    отдаём последний ответ и идём проверять стрим.
    """
    last = "not_allowed"
    while True:
        state = finish_spawn(bot)
        if state in ("sent", "already"):
            return state
        if state == "offline":
            return state
        last = state
        if time.monotonic() >= deadline:
            return last
        time.sleep(0.5)


def ensure(bot, *, timeout: float = PROOF_TIMEOUT, enter_timeout: float = ENTER_TIMEOUT,
           fallback_warp: bool = True, log=None) -> dict:
    """Довести бота до общего мира. Возвращает отчёт, а не молчание."""
    started = time.monotonic()
    deadline = started + max(1.0, enter_timeout)

    # 1. Ждём игру.
    while time.monotonic() < deadline:
        if position(bot) is not None:
            break
        if not getattr(bot.session, "connected", False):
            time.sleep(POLL)
            continue
        time.sleep(POLL)
    pos = position(bot)
    if pos is None:
        return {"status": "not_in_game", "players": [], "position": None}

    # 2. Финальное подтверждение после меню. Это единственный Spawn RPC
    # после реального ответа вокзала; request_class-хендшейк по-прежнему
    # выключен в logic.spawn.py.
    #
    # Ждём РАЗРЕШЕНИЯ, а не первую попавшуюся позицию. Раньше здесь стоял
    # один вызов сразу после появления координат: сервер к этому моменту
    # ещё не выставил spawn_response, finish_spawn отвечал not_allowed,
    # и второго шанса не было. Стрим оставался пустым навсегда, и бот
    # честно рапортовал «мир не подтверждён», хотя ему просто не отправили
    # Spawn. Опрос безопасен: отправка внутри защищена _rpc_spawn_queued,
    # поэтому RPC уходит ровно один раз за вход.
    spawn_state = _wait_spawn_allowed(
        bot, time.monotonic() + SPAWN_WAIT, log=log)
    if spawn_state == "sent":
        _log(log, "[МИР] финальный Spawn отправлен после меню")

    # 3. Ждём других игроков.
    players = wait_players(bot, timeout, log=log)
    if players:
        _log(log, f"[МИР] подтверждён: рядом {len(players)} игрок(ов)")
        return {
            "status": "confirmed",
            "players": players,
            "position": list(pos),
            "spawn": spawn_state,
            "seconds": round(time.monotonic() - started, 1),
        }

    # 4. Стрим пуст — идём на вокзал и проверяем ещё раз.
    if fallback_warp:
        moved = warp_to_station(bot, log=log)
        if moved.get("ok"):
            players = wait_players(bot, timeout, log=log)
            if players:
                _log(log, f"[МИР] подтверждён после перехода: рядом {len(players)}")
                return {
                    "status": "confirmed",
                    "players": players,
                    "position": list(position(bot) or pos),
                    "spawn": spawn_state,
                    "station": moved.get("station"),
                    "seconds": round(time.monotonic() - started, 1),
                }

    return {
        "status": "unconfirmed",
        "players": [],
        "position": list(position(bot) or pos),
        "spawn": spawn_state,
        "seconds": round(time.monotonic() - started, 1),
    }


def ensure_async(bot, *, log=None) -> threading.Thread:
    """Запустить проверку в фоне: панель не должна ждать 40 секунд."""
    def run():
        try:
            ensure(bot, log=log)
        except Exception:
            pass

    thread = threading.Thread(
        target=run, daemon=True,
        name=f"real-world-{getattr(bot, 'bot_id', '?')}",
    )
    thread.start()
    return thread
