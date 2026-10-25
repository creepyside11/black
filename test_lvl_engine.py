"""Движок качки: мир, вокзал, точка, /bg.

Проверяется то, из-за чего бот раньше просто стоял: без подтверждённого
мира он не идёт на точку, без координат не телепортируется в ноль, а
точка выбирается случайно и не повторяет предыдущую.
"""

import json
import os
import random
import tempfile
import time
import unittest
from unittest import mock

from logic import lvl_engine


class FakeApi:
    def __init__(self, pos=(100.0, 200.0, 10.0)):
        self._pos = pos
        self.chat = []

    def getbotposition(self):
        return self._pos

    def sendchat(self, msg):
        self.chat.append(msg)


class FakeSession:
    def __init__(self):
        self.connected = True


class FakeBot:
    def __init__(self, bot_id=1, pos=(100.0, 200.0, 10.0)):
        self.bot_id = bot_id
        self.api = FakeApi(pos)
        self.session = FakeSession()
        self.logs = []
        self.lvl_phase = ""
        self.lvl_target = None

    def add_log(self, message):
        self.logs.append(message)


class TargetsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lvl_targets_")
        self.path = os.path.join(self.tmp, "lvl_targets.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_is_empty_not_crash(self):
        self.assertEqual(lvl_engine.load_targets(self.path), {})

    def test_broken_file_is_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        self.assertEqual(lvl_engine.load_targets(self.path), {})

    def test_roundtrip(self):
        spawns = {"Нижегородск 2": [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)]}
        lvl_engine.save_targets(spawns, self.path)
        loaded = lvl_engine.load_targets(self.path)
        self.assertEqual(loaded["Нижегородск 2"][0], (1.0, 2.0, 3.0))
        self.assertEqual(len(loaded["Нижегородск 2"]), 2)

    def test_bad_points_are_dropped(self):
        payload = {"spawns": {"A": [[1, 2, 3], ["x"], [4, 5, 6]]}}
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        loaded = lvl_engine.load_targets(self.path)
        self.assertEqual(len(loaded["A"]), 2)


class PickTargetTests(unittest.TestCase):
    def setUp(self):
        lvl_engine._LAST_PICK.clear()

    def test_no_points_returns_none(self):
        index, point = lvl_engine.pick_target(FakeBot(), "A", spawns={"A": []})
        self.assertIsNone(index)
        self.assertIsNone(point)

    def test_unknown_station_returns_none(self):
        index, point = lvl_engine.pick_target(FakeBot(), "B", spawns={"A": [(1, 2, 3)]})
        self.assertIsNone(point)

    def test_single_point_always_used(self):
        index, point = lvl_engine.pick_target(
            FakeBot(), "A", spawns={"A": [(7.0, 8.0, 9.0)]})
        self.assertEqual(index, 0)
        self.assertEqual(point, (7.0, 8.0, 9.0))

    def test_does_not_repeat_previous(self):
        spawns = {"A": [(1, 0, 0), (2, 0, 0), (3, 0, 0)]}
        bot = FakeBot(bot_id=7)
        first, _ = lvl_engine.pick_target(bot, "A", spawns=spawns)
        second, _ = lvl_engine.pick_target(bot, "A", spawns=spawns)
        self.assertNotEqual(first, second)

    def test_choice_spreads_across_all_points(self):
        spawns = {"A": [(1, 0, 0), (2, 0, 0), (3, 0, 0)]}
        seen = set()
        for i in range(1, 40):
            lvl_engine._LAST_PICK.clear()
            index, _ = lvl_engine.pick_target(
                FakeBot(bot_id=i), "A", spawns=spawns, rng=random.Random(i))
            seen.add(index)
        self.assertEqual(seen, {0, 1, 2})


class LevelRunTests(unittest.TestCase):
    def setUp(self):
        lvl_engine._LAST_PICK.clear()

    def _run(self, bot, spawns, *, delay=0.0, world=None, warp=None):
        run = lvl_engine.LevelRun(
            bot, log=bot.add_log, spawn_delay=delay,
            world_timeout=0.2, spawns=spawns,
        )
        with mock.patch.object(lvl_engine, "current_station", return_value="Нижегородск 2"):
            with mock.patch("logic.real_world.ensure", return_value=world or {
                "status": "confirmed", "players": [{"name": "Alpha"}],
            }):
                with mock.patch.object(lvl_engine, "travel_to_point",
                                return_value=warp or {"status": "ok",
                                                      "final_position": (500.0, 600.0, 10.0)}):
                    with mock.patch("logic.immortal.enable") as imm:
                        run.run_once()
        return run, imm

    def test_happy_path_reaches_point_and_sends_command(self):
        bot = FakeBot()
        spawns = {"Нижегородск 2": [(500.0, 600.0, 10.0)]}
        run, imm = self._run(bot, spawns)
        self.assertEqual(run.report["status"], "done")
        self.assertEqual(run.report["station"], "Нижегородск 2")
        self.assertEqual(bot.api.chat, ["/bg"])
        self.assertTrue(imm.called, "бессмертие должно включаться")
        self.assertEqual(bot.lvl_target, [500.0, 600.0, 10.0])

    def test_reports_spawn_name_in_log(self):
        bot = FakeBot()
        self._run(bot, {"Нижегородск 2": [(1.0, 2.0, 3.0)]})
        joined = "\n".join(bot.logs)
        self.assertIn("СПАВН", joined)
        self.assertIn("Нижегородск 2", joined)

    def test_unconfirmed_world_stops_before_warp(self):
        bot = FakeBot()
        with mock.patch.object(lvl_engine, "travel_to_point") as warp:
            run, _ = self._run(bot, {"Нижегородск 2": [(1.0, 2.0, 3.0)]},
                               world={"status": "unconfirmed", "players": []})
        self.assertEqual(run.report["status"], "world_unconfirmed")
        warp.assert_not_called()
        self.assertEqual(bot.api.chat, [])

    def test_no_coordinates_does_not_teleport(self):
        bot = FakeBot()
        with mock.patch.object(lvl_engine, "travel_to_point") as warp:
            run, _ = self._run(bot, {"Нижегородск 2": []})
        self.assertEqual(run.report["status"], "no_targets")
        warp.assert_not_called()
        self.assertEqual(bot.api.chat, [])

    def test_short_warp_skips_command(self):
        bot = FakeBot()
        run, _ = self._run(bot, {"Нижегородск 2": [(1.0, 2.0, 3.0)]},
                           warp={"status": "ok",
                                 "final_position": (450.0, 600.0, 10.0)})
        self.assertEqual(run.report["status"], "short")
        self.assertEqual(bot.api.chat, [])

    def test_delay_can_be_stopped(self):
        """Стоп во время минуты ожидания выходит сразу, а не через 30с."""
        import threading
        bot = FakeBot()
        run = lvl_engine.LevelRun(bot, log=bot.add_log, spawn_delay=30.0,
                                  spawns={"Нижегородск 2": [(1.0, 2.0, 3.0)]})
        run._stop.set()
        started = time.monotonic()
        with mock.patch.object(lvl_engine, "current_station", return_value="Нижегородск 2"):
            with mock.patch("logic.real_world.ensure",
                            return_value={"status": "confirmed",
                                          "players": [{"name": "A"}]}):
                with mock.patch.object(lvl_engine, "travel_to_point") as warp:
                    report = run.run_once()
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(report["status"], "stopped")
        warp.assert_not_called()

    def test_stop_prevents_command(self):
        bot = FakeBot()
        run = lvl_engine.LevelRun(bot, log=bot.add_log, spawn_delay=0.0,
                                  spawns={"Нижегородск 2": [(1.0, 2.0, 3.0)]})
        run._stop.set()
        with mock.patch.object(lvl_engine, "current_station", return_value="Нижегородск 2"):
            with mock.patch("logic.real_world.ensure",
                            return_value={"status": "confirmed", "players": [{"name": "A"}]}):
                with mock.patch("logic.immortal.enable"):
                    run.run_once()
        self.assertEqual(bot.api.chat, [])


class TravelSpeedTests(unittest.TestCase):
    """Переход идёт delta-методом на честной скорости, а не прыжками.

    Раньше стоял pause_warp кусками по 45 м с паузой 0.4 с — это 112 м/с.
    Сервер такие прыжки откатывает, и бот остаётся на вокзале.
    """

    def test_default_speed_is_eight(self):
        self.assertAlmostEqual(lvl_engine.TRAVEL_SPEED, 8.0)

    def test_step_matches_speed_and_interval(self):
        from logic.delta_teleport import step_for_speed
        step = step_for_speed(lvl_engine.TRAVEL_SPEED, lvl_engine.TRAVEL_INTERVAL)
        speed = step / lvl_engine.TRAVEL_INTERVAL
        self.assertAlmostEqual(speed, 8.0, places=3)

    def test_engine_uses_delta_not_pause_warp(self):
        with open(lvl_engine.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("DeltaTeleport", source)
        self.assertNotIn("pause_warp(", source,
                         "движок снова прыгает через pause_warp")

    def test_travel_builds_engine_with_safe_speed(self):
        bot = FakeBot()
        captured = {}

        class FakeEngine:
            def __init__(self, player, sender, **kwargs):
                captured.update(kwargs)

            def run(self, target):
                return {"status": "ok", "final_position": target,
                        "distance": 0.0, "elapsed": 1.0}

        bot.session.players = type("P", (), {"bot": object()})()
        bot.api._delta_sync_sender = object()
        bot.session.sync = type("Sync", (), {"enabled": False,
                                             "start": lambda self: None,
                                             "stop": lambda self: None})()
        with mock.patch("logic.delta_teleport.DeltaTeleport", FakeEngine):
            out = lvl_engine.travel_to_point(bot, (10.0, 20.0, 5.0))
        self.assertEqual(out["status"], "ok")
        speed = captured["step"] / captured["interval"]
        self.assertAlmostEqual(speed, lvl_engine.TRAVEL_SPEED, places=2)
        self.assertEqual(captured["velocity_mode"], "consistent")


class TaskTests(unittest.TestCase):
    """Задача `lvl` в боте должна вести на точку, а не стоять на спавне."""

    def setUp(self):
        lvl_engine._LAST_PICK.clear()

    def _task(self, bot=None):
        from logic.bot_tasks import LevelRunnerTask
        bot = bot or FakeBot()
        task = LevelRunnerTask(bot)
        task.DRIFT_LIMIT = 30.0
        task.HOLD_POLL = 0.01
        return task, bot

    def test_task_is_wired_to_engine(self):
        from logic import bot_tasks
        with open(bot_tasks.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("LevelRun", source)
        self.assertIn("run_once", source)
        self.assertIn("travel_to_point", source)

    def test_cycle_goes_to_point_and_sends_bg(self):
        task, bot = self._task()
        report = {"status": "done", "station": "Нижегородск 2",
                  "target": [-2125.7, 1311.7, 12.0], "index": 1,
                  "players": 17, "command_sent": True}
        with mock.patch.object(task, "_cycle", return_value=report):
            with mock.patch.object(task, "_hold") as hold:
                task.running = True
                task._iterate()
        self.assertTrue(hold.called, "после /bg точка должна держаться")
        joined = "\n".join(bot.logs)
        self.assertIn("НА ТОЧКЕ", joined)
        self.assertIn("Нижегородск 2", joined)
        self.assertIn("точка 2", joined)

    def test_log_lines_carry_nickname(self):
        task, bot = self._task()
        bot.nickname = "Gennady_Phoenixxxx"
        task._log("[LVL] тест")
        self.assertIn("Gennady_Phoenixxxx", bot.logs[-1])

    def test_hold_returns_bot_to_point_when_drifted(self):
        task, bot = self._task()
        task.HOLD_POLL = 0.01
        bot.api.getbotposition = lambda: (100.0, 100.0, 10.0)
        point = (500.0, 600.0, 10.0)
        calls = {"n": 0}

        def fake_travel(_bot, target, **kwargs):
            calls["n"] += 1
            task.running = False  # один проход и выходим
            return {"status": "ok", "final_position": target}

        with mock.patch.object(lvl_engine, "travel_to_point", fake_travel):
            task.running = True
            task._hold({"target": list(point), "station": "Нижегородск 2"})
        self.assertEqual(calls["n"], 1, "снесло с точки — обязан вернуться")
        self.assertEqual(bot.api.chat[-1], "/bg", "после возврата /bg повторяется")

    def test_hold_does_not_move_when_on_point(self):
        task, bot = self._task()
        task.HOLD_POLL = 0.01
        point = (500.0, 600.0, 10.0)

        def position_once():
            task.running = False  # один проход и выходим
            return (505.0, 600.0, 10.0)

        bot.api.getbotposition = position_once

        def fake_travel(*_a, **_k):
            raise AssertionError("стоящего на точке двигать нельзя")

        with mock.patch.object(lvl_engine, "travel_to_point", fake_travel):
            task.running = True
            task._hold({"target": list(point), "station": "Нижегородск 2"})
        self.assertEqual(bot.api.chat, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
