"""Бессмертие от голода.

Сервер присылает «Вы проголодались, здоровье ухудшается» и потихоньку
снимает HP. Клиент в каждом sync-пакете сообщает своё здоровье, поэтому
достаточно держать его на максимуме — голод перестаёт убивать.

Флаг godmode_zero_xp заставляет пакет уходить со 100 HP даже если
локально здоровье успело просесть между тиками.
"""

from __future__ import annotations

import threading
import time

MAX_HEALTH = 100.0
TICK = 0.25


def _player(bot):
    try:
        return bot.session.players.bot
    except Exception:
        return None


def apply(bot, health: float = MAX_HEALTH) -> bool:
    """Один тик: поднять HP и включить подмену в пакете."""
    player = _player(bot)
    if player is None:
        return False
    try:
        player.health = float(health)
        player.godmode_zero_xp = True
    except Exception:
        return False
    return True


def disable(bot) -> bool:
    player = _player(bot)
    if player is None:
        return False
    try:
        player.godmode_zero_xp = False
    except Exception:
        return False
    return True


class ImmortalGuard:
    """Фоновый сторож: держит здоровье на максимуме, пока не остановят."""

    def __init__(self, bot, health: float = MAX_HEALTH, tick: float = TICK, log=print):
        self.bot = bot
        self.health = float(health)
        self.tick = max(0.05, float(tick))
        self.log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.applied = 0

    def start(self) -> "ImmortalGuard":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="immortal-guard", daemon=True
        )
        self._thread.start()
        self.log(f"[immortal] включено, HP держится на {self.health:.0f}")
        return self

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        disable(self.bot)
        self.log("[immortal] выключено")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            if apply(self.bot, self.health):
                self.applied += 1
            self._stop.wait(self.tick)


_GUARDS: dict[int, "ImmortalGuard"] = {}
_LOCK = threading.Lock()


def _key(bot) -> int:
    try:
        return int(getattr(bot, "bot_id", 0) or 0)
    except Exception:
        return 0


def enable(bot, health: float = MAX_HEALTH, log=print) -> ImmortalGuard:
    """Включить бессмертие от голода для конкретного бота.

    Раньше сторож был один на процесс: включение на втором боте
    перезаписывало ссылку, первый оставался без присмотра, а stop()
    глушил чужого. На ферме это значит, что бессмертие живёт только у
    последнего запущенного. Теперь сторожей столько, сколько ботов.
    """
    key = _key(bot)
    with _LOCK:
        guard = _GUARDS.get(key)
        if guard is not None and guard.running and guard.bot is bot:
            apply(bot, health)
            return guard
        guard = ImmortalGuard(bot, health=health, log=log).start()
        _GUARDS[key] = guard
        return guard


def stop(bot=None, log=print) -> None:
    """Выключить бессмертие у одного бота или у всех сразу."""
    with _LOCK:
        if bot is None:
            guards = list(_GUARDS.values())
            _GUARDS.clear()
        else:
            guard = _GUARDS.pop(_key(bot), None)
            guards = [guard] if guard is not None else []
    for guard in guards:
        guard.stop()


def status(bot) -> dict:
    player = _player(bot)
    with _LOCK:
        guard = _GUARDS.get(_key(bot))
    return {
        "running": bool(guard and guard.running),
        "health": float(getattr(player, "health", 0.0)) if player else 0.0,
        "godmode": bool(getattr(player, "godmode_zero_xp", False)) if player else False,
        "ticks": guard.applied if guard else 0,
    }
