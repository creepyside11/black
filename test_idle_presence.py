"""Проверки режима присутствия: бот топчется на спавне и никуда не едет."""

import math
import random
import unittest

from logic.idle_presence import (
    PAUSE_MAX,
    PAUSE_MIN,
    STAND_STILL_CHANCE,
    STEP_MAX,
    STEP_MIN,
    STAY_RADIUS,
    IdlePresence,
    describe,
)


class FakeApi:
    """Считает вызовы ходьбы и отдаёт заранее заданную позицию."""

    def __init__(self, pos=(100.0, 200.0, 10.0)):
        self.pos = pos
        self.calls = []
        self.fail = False

    def getbotposition(self):
        return self.pos

    def MoveToCoord(self, x, y, z, **kw):
        if self.fail:
            raise RuntimeError("нет сессии")
        self.calls.append((x, y, z, kw))
        self.pos = (x, y, z)


def make(pos=(100.0, 200.0, 10.0), seed=1, **kw):
    api = FakeApi(pos)
    slept = []
    said = []
    p = IdlePresence(
        api,
        rng=random.Random(seed),
        log=said.append,
        sleep=slept.append,
        **kw,
    )
    return p, api, slept, said


class StepTests(unittest.TestCase):
    def test_walk_uses_walk_mode(self):
        p, api, _slept, _said = make()
        p.rng = random.Random(7)
        # Прогоняем, пока не выпадет шаг, а не простой.
        for _ in range(50):
            api.calls.clear()
            p.step()
            if api.calls:
                break
        self.assertTrue(api.calls, "за 50 ходов шаг так и не выпал")
        _x, _y, _z, kw = api.calls[0]
        self.assertEqual(kw.get("mode"), "walk")

    def test_step_length_stays_within_bounds(self):
        p, api, _slept, _said = make()
        p.remember_anchor((100.0, 200.0, 10.0))
        for _ in range(200):
            target = p.pick_target(p.position())
            d = math.hypot(target[0] - 100.0, target[1] - 200.0)
            self.assertLessEqual(d, STAY_RADIUS + 1e-6)
            if api.calls:
                step = math.hypot(target[0] - 100.0, target[1] - 200.0)
                self.assertLessEqual(step, STEP_MAX + 1e-6)

    def test_never_leaves_the_radius(self):
        # Ставим бота у самой границы и гоняем: за радиус выйти нельзя.
        p, _api, _slept, _said = make()
        p.remember_anchor((100.0, 200.0, 10.0))
        pos = (100.0 + STAY_RADIUS, 200.0, 10.0)
        for _ in range(300):
            target = p.pick_target(pos)
            d = math.hypot(target[0] - 100.0, target[1] - 200.0)
            self.assertLessEqual(d, STAY_RADIUS + 1e-6)
            pos = target

    def test_stand_still_does_not_move(self):
        p, api, slept, _said = make()
        p.rng = random.Random(3)
        # Ищем ход, где бот просто стоит.
        for _ in range(80):
            api.calls.clear()
            slept.clear()
            p.step()
            if not api.calls:
                break
        self.assertFalse(api.calls, "ни одного простоя не выпало")
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], PAUSE_MIN)
        self.assertLessEqual(slept[0], PAUSE_MAX)

    def test_pause_is_slept_after_a_step(self):
        p, api, slept, _said = make()
        p.rng = random.Random(11)
        for _ in range(60):
            api.calls.clear()
            slept.clear()
            p.step()
            if api.calls:
                break
        self.assertTrue(api.calls)
        self.assertEqual(len(slept), 1, "после шага должна быть ровно одна пауза")
        self.assertGreaterEqual(slept[0], PAUSE_MIN)

    def test_no_position_is_not_a_crash(self):
        p, api, _slept, said = make()
        api.getbotposition = lambda: None
        self.assertEqual(p.step(), "нет позиции")

    def test_dead_api_is_reported_not_raised(self):
        p, api, slept, said = make()
        p.rng = random.Random(5)
        api.fail = True
        for _ in range(50):
            p.step()
            if any("не удался" in m for m in said):
                break
        self.assertTrue(any("не удался" in m for m in said))
        self.assertTrue(slept)


class AnchorTests(unittest.TestCase):
    def test_anchor_is_remembered_once(self):
        p, _api, _slept, said = make()
        p.remember_anchor((1.0, 2.0, 3.0))
        p.remember_anchor((9.0, 9.0, 9.0))
        self.assertEqual(p.anchor, (1.0, 2.0, 3.0))
        self.assertTrue(any("точка топтания" in m for m in said))

    def test_anchor_taken_from_position_when_not_given(self):
        p, _api, _slept, _said = make(pos=(5.0, 6.0, 7.0))
        p.remember_anchor()
        self.assertEqual(p.anchor, (5.0, 6.0, 7.0))


class RunTests(unittest.TestCase):
    def test_run_respects_max_steps(self):
        p, _api, _slept, _said = make()
        res = p.run(max_steps=4)
        self.assertEqual(res["moves"] + res["stands"], 4)

    def test_run_stops_on_should_stop(self):
        p, _api, _slept, _said = make()
        state = {"n": 0}

        def stop():
            state["n"] += 1
            return state["n"] > 3

        p.run(should_stop=stop)
        self.assertLessEqual(p.moves + p.stands, 3)

    def test_run_reports_moves_and_stands(self):
        p, _api, _slept, _said = make()
        res = p.run(max_steps=30)
        self.assertEqual(res["moves"] + res["stands"], 30)
        self.assertGreaterEqual(res["seconds"], 0)


class DescribeTests(unittest.TestCase):
    def test_describe_mentions_the_limits(self):
        text = describe()
        self.assertIn(f"{STEP_MIN:.1f}", text)
        self.assertIn(f"{STAY_RADIUS:.0f}", text)


class SanityTests(unittest.TestCase):
    def test_bounds_are_sane(self):
        self.assertGreater(STEP_MIN, 0.0)
        self.assertLess(STEP_MIN, STEP_MAX)
        self.assertGreater(PAUSE_MIN, 0.0)
        self.assertLess(PAUSE_MIN, PAUSE_MAX)
        self.assertTrue(0.0 < STAND_STILL_CHANCE < 1.0)
        # Радиус должен быть заметно больше самого длинного шага, иначе
        # бот будет вечно тереться об границу.
        self.assertGreater(STAY_RADIUS, STEP_MAX * 2)


if __name__ == "__main__":
    unittest.main()
