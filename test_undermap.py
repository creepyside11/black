"""Под картой: маршрут, скорость, изоляция от поверхности."""

import math
import unittest

from logic import undermap
from logic.delta_teleport import SAFE_SPEED


class FakePlayer:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self._x, self._y, self._z = float(x), float(y), float(z)
        self.velocity_x = self.velocity_y = self.velocity_z = 0.0
        self.rotation = 0.0
        self.keys = 0

    def get_position(self):
        return (self._x, self._y, self._z)

    def _set_position_internal(self, x, y, z, *, update_velocity=True, now=None):
        self._x, self._y, self._z = float(x), float(y), float(z)

    def set_rotation(self, rotation):
        self.rotation = float(rotation)

    def set_keys(self, keys):
        self.keys = int(keys)

    def reset_velocity(self):
        self.velocity_x = self.velocity_y = self.velocity_z = 0.0


class Recorder:
    def __init__(self, player):
        self.player = player
        self.packets = []

    def __call__(self):
        self.packets.append(
            {
                "pos": self.player.get_position(),
                "vel": (
                    self.player.velocity_x,
                    self.player.velocity_y,
                    self.player.velocity_z,
                ),
            }
        )


class FakeSync:
    def __init__(self):
        self.enabled = True
        self.stops = 0
        self.starts = 0

    def stop(self):
        self.enabled = False
        self.stops += 1

    def start(self):
        self.enabled = True
        self.starts += 1

    def send_player_sync(self):
        pass


class FakeSession:
    def __init__(self, player):
        self.connected = True
        self.players = type("P", (), {"bot": player})()
        self.sync = FakeSync()


class FakeApi:
    def __init__(self, recorder):
        self._delta_sync_sender = recorder


class FakeBot:
    def __init__(self, player):
        self.session = FakeSession(player)
        self.api = FakeApi(Recorder(player))


def run_route(start, target, **kwargs):
    player = FakePlayer(*start)
    bot = FakeBot(player)
    recorder = bot.api._delta_sync_sender
    report = undermap.travel_undermap(
        bot,
        target,
        interval=0.001,
        settle_packets=0,
        force=kwargs.pop("force", True),
        sleep=lambda _seconds: None,
        **kwargs,
    )
    return report, recorder, player, bot


class PlanTests(unittest.TestCase):
    def test_three_legs_for_surface_trip(self):
        legs = undermap.plan_legs((0, 0, 20), (500, 0, 20))
        self.assertEqual([n for n, _ in legs],
                         [undermap.LEG_DOWN, undermap.LEG_ACROSS, undermap.LEG_UP])

    def test_down_keeps_x_and_y(self):
        legs = undermap.plan_legs((10, 20, 30), (500, 600, 40))
        name, point = legs[0]
        self.assertEqual(name, undermap.LEG_DOWN)
        self.assertEqual(point[0], 10)
        self.assertEqual(point[1], 20)

    def test_across_keeps_depth(self):
        legs = undermap.plan_legs((0, 0, 20), (500, 0, 20))
        name, point = legs[1]
        self.assertEqual(name, undermap.LEG_ACROSS)
        self.assertEqual(point[2], undermap.UNDERMAP_DEPTH)

    def test_up_ends_exactly_on_target(self):
        legs = undermap.plan_legs((0, 0, 20), (500, 700, 40))
        name, point = legs[-1]
        self.assertEqual(name, undermap.LEG_UP)
        self.assertEqual(point, (500.0, 700.0, 40.0))

    def test_vertical_only_trip_skips_across(self):
        legs = undermap.plan_legs((5, 5, 100), (5, 5, 20))
        self.assertEqual([n for n, _ in legs],
                         [undermap.LEG_DOWN, undermap.LEG_UP])

    def test_already_below_depth_does_not_go_up_first(self):
        legs = undermap.plan_legs((0, 0, -200), (100, 0, -200))
        # floor = min(-80, -200, -200) = -200, спуска нет
        self.assertEqual([n for n, _ in legs], [undermap.LEG_ACROSS])

    def test_custom_depth(self):
        legs = undermap.plan_legs((0, 0, 20), (100, 0, 20), depth=-40.0)
        self.assertEqual(legs[1][1][2], -40.0)


class EstimateTests(unittest.TestCase):
    def test_undermap_costs_two_depths_extra(self):
        est = undermap.estimate((0, 0, 20), (500, 0, 20))
        direct = 500.0
        expected = (20 - undermap.UNDERMAP_DEPTH) * 2 + 500
        self.assertAlmostEqual(est["direct_distance"], direct, places=3)
        self.assertAlmostEqual(est["distance"], expected, places=3)
        self.assertGreater(est["overhead"], 0)

    def test_seconds_at_safe_speed(self):
        est = undermap.estimate((0, 0, 20), (500, 0, 20))
        self.assertAlmostEqual(est["seconds"], est["distance"] / SAFE_SPEED, places=3)

    def test_short_trip_flagged(self):
        self.assertFalse(undermap.estimate((0, 0, 20), (10, 0, 20))["needs_undermap"])

    def test_long_trip_flagged(self):
        self.assertTrue(undermap.estimate((0, 0, 20), (500, 0, 20))["needs_undermap"])


class TravelTests(unittest.TestCase):
    def test_ends_on_target(self):
        report, _, player, _ = run_route((0, 0, 20), (200, 0, 20))
        self.assertEqual(report["status"], "ok")
        x, y, z = player.get_position()
        self.assertAlmostEqual(x, 200, places=1)
        self.assertAlmostEqual(z, 20, places=1)

    def test_never_visible_above_depth_mid_route(self):
        """На горизонтальном участке высота должна быть на глубине."""
        report, recorder, _, _ = run_route((0, 0, 20), (300, 0, 20))
        self.assertEqual(report["status"], "ok")
        across = [p for p in recorder.packets
                  if abs(p["pos"][2] - undermap.UNDERMAP_DEPTH) < 0.5]
        self.assertGreater(len(across), 100)
        # и на этом участке мы реально прошли по x
        xs = [p["pos"][0] for p in across]
        self.assertGreater(max(xs) - min(xs), 200)

    def test_step_never_exceeds_plan(self):
        """Ни один шаг не больше расчётного: превышение — это и есть спидчек.

        Последний шаг участка бывает короче (остаток дистанции) — это в
        сторону медленнее, для анти-чита безопасно.
        """
        report, recorder, _, _ = run_route((0, 0, 20), (200, 0, 20))
        self.assertEqual(report["status"], "ok")
        steps = []
        prev = recorder.packets[0]["pos"]
        for packet in recorder.packets[1:]:
            pos = packet["pos"]
            step = math.dist(prev, pos)
            if step > 1e-9:
                steps.append(step)
            prev = pos
        self.assertTrue(steps)
        self.assertLessEqual(max(steps), 0.171)
        # подавляющая часть шагов — ровно расчётные
        self.assertGreater(sum(1 for x in steps if x > 0.169), len(steps) * 0.9)

    def test_velocity_never_exceeds_safe(self):
        report, recorder, _, _ = run_route((0, 0, 20), (200, 0, 20))
        self.assertEqual(report["status"], "ok")
        for packet in recorder.packets:
            speed = math.sqrt(sum(v * v for v in packet["vel"])) * 50.0
            self.assertLessEqual(speed, SAFE_SPEED + 0.5)

    def test_sync_restored_after_travel(self):
        report, _, _, bot = run_route((0, 0, 20), (200, 0, 20))
        self.assertEqual(report["status"], "ok")
        self.assertTrue(bot.session.sync.enabled)
        self.assertEqual(bot.session.sync.starts, 1)

    def test_short_trip_skipped_without_force(self):
        player = FakePlayer(0, 0, 20)
        bot = FakeBot(player)
        report = undermap.travel_undermap(bot, (10, 0, 20), interval=0.001,
                                          force=False,
                                          sleep=lambda _seconds: None)
        self.assertEqual(report["status"], "too_short")
        self.assertEqual(bot.session.sync.stops, 0)

    def test_abort_on_disconnect(self):
        player = FakePlayer(0, 0, 20)
        bot = FakeBot(player)
        calls = {"n": 0}

        def abort():
            calls["n"] += 1
            return calls["n"] > 20

        report = undermap.travel_undermap(
            bot, (500, 0, 20), interval=0.001, settle_packets=0,
            force=True, abort_check=abort, sleep=lambda _seconds: None,
        )
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(report["reason"], "session_down")

    def test_report_has_all_legs(self):
        report, _, _, _ = run_route((0, 0, 20), (200, 0, 20))
        self.assertEqual(len(report["legs"]), 3)
        self.assertEqual([l["leg"] for l in report["legs"]],
                         [undermap.LEG_DOWN, undermap.LEG_ACROSS, undermap.LEG_UP])

    def test_elapsed_is_sum_of_legs(self):
        report, _, _, _ = run_route((0, 0, 20), (200, 0, 20))
        self.assertAlmostEqual(
            report["elapsed"], sum(l["elapsed"] for l in report["legs"]), places=6
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
