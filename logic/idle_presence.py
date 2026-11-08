"""Присутствие: бот стоит на спавне и чуть шевелится, как живой игрок.

Это не сбор и не фарм. Задача ровно одна — аккаунт выглядит как человек,
который зашёл в игру и топчется у вокзала: короткие перебежки на пару
метров, длинные паузы, иногда просто стоит. Ничего не поднимает, не
открывает меню, никуда не едет.

Зачем отдельно от сборщика: у сбора есть маршрут, телепорт и риск, а тут
риска нет вовсе. Если аккаунт нужно просто «подержать в мире» — это оно.
"""

import math
import random
import time

# Пауза между перебежками. Живой человек у вокзала стоит дольше, чем идёт,
# поэтому нижняя граница заметно выше нуля.
PAUSE_MIN = 3.0
PAUSE_MAX = 18.0

# Иногда не идём никуда, а просто стоим. Доля таких «ходов».
STAND_STILL_CHANCE = 0.35

# Длина перебежки в метрах: два шага туда, два обратно.
STEP_MIN = 1.5
STEP_MAX = 6.0

# Дальше этого от точки спавна не уходим: смысл режима — топтаться на
# месте, а не уезжать потихоньку в город.
STAY_RADIUS = 20.0

# Ходьба, а не бег: телепорт в этом режиме не нужен вовсе.
WALK_MODE = "walk"


class IdlePresence:
    """Топтание на месте вокруг точки спавна.

    Всё случайное проведено через rng, время — через sleep и clock, чтобы
    поведение можно было проверить тестом без ожидания в реальном времени.
    """

    def __init__(
        self,
        api,
        *,
        anchor=None,
        rng=None,
        log=None,
        sleep=time.sleep,
        clock=time.monotonic,
        radius=STAY_RADIUS,
    ):
        self.api = api
        self.rng = rng or random.Random()
        self.log = log or (lambda _m: None)
        self.sleep = sleep
        self.clock = clock
        self.radius = float(radius)
        self.anchor = tuple(anchor) if anchor else None
        self.moves = 0
        self.stands = 0
        self.started_at = self.clock()

    # --- служебное ---

    def position(self):
        try:
            pos = self.api.getbotposition()
        except Exception:
            return None
        if not pos or len(pos) < 3:
            return None
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    def remember_anchor(self, pos=None):
        """Запомнить точку спавна, вокруг которой топчемся."""
        if self.anchor is None:
            pos = pos or self.position()
            if pos:
                self.anchor = pos
                self.log(f"точка топтания: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})")
        return self.anchor

    # --- один ход ---

    def pick_target(self, pos):
        """Куда шагнуть из текущей точки, не выходя за радиус."""
        if self.anchor is None:
            self.remember_anchor(pos)
        ax, ay, _az = self.anchor

        angle = self.rng.uniform(0.0, 2.0 * math.pi)
        length = self.rng.uniform(STEP_MIN, STEP_MAX)
        tx = pos[0] + length * math.cos(angle)
        ty = pos[1] + length * math.sin(angle)

        # Если шаг уводит за радиус — тянем его назад, к центру.
        dx, dy = tx - ax, ty - ay
        dist = math.hypot(dx, dy)
        if dist > self.radius:
            tx = ax + dx / dist * self.radius
            ty = ay + dy / dist * self.radius
        return (tx, ty, pos[2])

    def step(self):
        """Один ход: либо стоим, либо короткая перебежка. Возвращает описание."""
        pos = self.position()
        if pos is None:
            return "нет позиции"
        self.remember_anchor(pos)

        if self.rng.random() < STAND_STILL_CHANCE:
            self.stands += 1
            pause = self.rng.uniform(PAUSE_MIN, PAUSE_MAX)
            self.log(f"стою {pause:.0f} с")
            self.sleep(pause)
            return "стою"

        target = self.pick_target(pos)
        moved = math.hypot(target[0] - pos[0], target[1] - pos[1])
        self.log(f"шагаю {moved:.1f} м -> ({target[0]:.1f}, {target[1]:.1f})")
        try:
            self.api.MoveToCoord(target[0], target[1], target[2], mode=WALK_MODE,
                                 completion_distance=0.5)
        except Exception as exc:
            self.log(f"    шаг не удался: {exc!r}")
            self.sleep(PAUSE_MIN)
            return "шаг не удался"

        self.moves += 1
        pause = self.rng.uniform(PAUSE_MIN, PAUSE_MAX)
        self.sleep(pause)
        return "шагнул"

    # --- цикл ---

    def run(self, should_stop=None, *, max_steps=None):
        """Топтаться, пока should_stop() не скажет «хватит»."""
        self.started_at = self.clock()
        self.log("режим присутствия: топчусь на спавне, никуда не еду")
        steps = 0
        while True:
            if should_stop is not None and should_stop():
                break
            if max_steps is not None and steps >= max_steps:
                break
            self.step()
            steps += 1
        done = self.clock() - self.started_at
        self.log(f"присутствие окончено: шагов {self.moves}, простоев {self.stands}, "
                 f"{done / 60:.1f} мин")
        return {"moves": self.moves, "stands": self.stands, "seconds": done}


def describe() -> str:
    """Строка для лога и --help: что за режим и чем отличается."""
    return (f"стою на спавне и топчусь: шаги {STEP_MIN:.1f}-{STEP_MAX:.1f} м, "
            f"паузы {PAUSE_MIN:.0f}-{PAUSE_MAX:.0f} с, "
            f"не дальше {STAY_RADIUS:.0f} м от спавна")
