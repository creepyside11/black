"""Маршрут сбора: координаты и геометрия.

Координаты сняты с живого клиента (route2.pcap). Тесты держат их
согласованными: если кто-то поправит точку и она улетит за пределы
площадки, тест это поймает.
"""

import time
import unittest

from logic import collect_route as cr


class PointTests(unittest.TestCase):
    def test_five_points(self):
        self.assertEqual(len(cr.WORK_POINTS), 5)

    def test_all_points_are_on_the_ground(self):
        for i, p in enumerate(cr.WORK_POINTS):
            self.assertTrue(cr.on_work_ground(p), f"точка #{i + 1} вне площадки")

    def test_points_are_near_the_anchor(self):
        for i, p in enumerate(cr.WORK_POINTS):
            self.assertLess(cr.distance2d(p, cr.WORK_ANCHOR), 120.0, f"точка #{i + 1}")

    def test_heights_are_plausible(self):
        for i, p in enumerate(cr.WORK_POINTS):
            self.assertGreater(p[2], 0.0)
            self.assertLess(p[2], 60.0)

    def test_round_is_about_250_metres(self):
        self.assertAlmostEqual(cr.round_length(), 249.0, delta=25.0)

    def test_points_do_not_overlap(self):
        for i in range(len(cr.WORK_POINTS)):
            for j in range(i + 1, len(cr.WORK_POINTS)):
                d = cr.distance(cr.WORK_POINTS[i], cr.WORK_POINTS[j])
                self.assertGreater(d, 15.0, f"точки {i + 1} и {j + 1} слишком близко")


class TravelTests(unittest.TestCase):
    def test_stations_need_travel(self):
        from logic.spawn import SPAWNS

        for name in ("Нижегородск 1", "Нижегородск 2"):
            self.assertTrue(cr.needs_travel(SPAWNS[name]), name)

    def test_station_distance_matches_capture(self):
        from logic.spawn import SPAWNS

        d1 = cr.distance(SPAWNS["Нижегородск 1"], cr.WORK_ANCHOR)
        d2 = cr.distance(SPAWNS["Нижегородск 2"], cr.WORK_ANCHOR)
        self.assertAlmostEqual(d1, 1168.0, delta=40.0)
        self.assertAlmostEqual(d2, 866.0, delta=40.0)

    def test_work_point_does_not_need_travel(self):
        self.assertFalse(cr.needs_travel(cr.WORK_POINTS[0]))

    def test_recorded_start_position_is_on_the_ground(self):
        # С этой точки началась запись — аккаунт вошёл на сохранённую
        # позицию, а не на вокзал.
        start = (-1444.90, 2130.31, 9.62)
        self.assertFalse(cr.needs_travel(start))
        self.assertLess(cr.distance(start, cr.WORK_ANCHOR), 80.0)

    def test_auth_zone_needs_travel(self):
        self.assertTrue(cr.needs_travel((13.7, 485.3, -20.0)))

    def test_broken_position_needs_travel(self):
        for bad in (None, (), (1.0, 2.0)):
            self.assertTrue(cr.needs_travel(bad))


class NextPointTests(unittest.TestCase):
    def test_standing_on_a_point_goes_to_the_next(self):
        i, p = cr.next_point(cr.WORK_POINTS[0])
        self.assertEqual(i, 1)
        self.assertEqual(p, cr.WORK_POINTS[1])

    def test_last_point_wraps_to_first(self):
        i, p = cr.next_point(cr.WORK_POINTS[-1])
        self.assertEqual(i, 0)
        self.assertEqual(p, cr.WORK_POINTS[0])

    def test_far_away_goes_to_the_nearest(self):
        # 9.5 м от точки #2 — ближе всех и дальше порога, поэтому идём
        # именно на неё, а не перескакиваем.
        i, p = cr.next_point((-1535.0, 2118.0, 9.6))
        self.assertEqual(i, 1)
        self.assertEqual(p, cr.WORK_POINTS[1])

    def test_standing_on_a_point_skips_it(self):
        # Стоим на точке #2 — она уже отработана, идём на #3.
        i, p = cr.next_point(cr.WORK_POINTS[1])
        self.assertEqual(i, 2)
        self.assertEqual(p, cr.WORK_POINTS[2])

    def test_no_position_returns_first(self):
        i, p = cr.next_point(None)
        self.assertEqual(i, 0)
        self.assertEqual(p, cr.WORK_POINTS[0])

    def test_empty_points_is_safe(self):
        self.assertEqual(cr.next_point((0, 0, 0), points=[]), (None, None))


class SummaryTests(unittest.TestCase):
    def test_recognises_both_stations(self):
        from logic.spawn import SPAWNS

        for name in ("Нижегородск 1", "Нижегородск 2"):
            info = cr.station_summary(SPAWNS[name])
            self.assertEqual(info["station"], name)
            self.assertTrue(info["needs_travel"])

    def test_unknown_place_is_described_by_coordinates(self):
        info = cr.station_summary((-1444.90, 2130.31, 9.62))
        self.assertIn("-1445", str(info["station"]))
        self.assertFalse(info["needs_travel"])

    def test_empty_position_is_safe(self):
        info = cr.station_summary(None)
        self.assertIsNone(info["station"])
        self.assertTrue(info["needs_travel"])

    def test_describe_route_renders(self):
        text = cr.describe_route()
        self.assertIn("точек: 5", text)
        self.assertEqual(text.count("#"), 5)


class RunnerTests(unittest.TestCase):
    def test_respawn_wait_is_at_least_two_minutes(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_collect", "run_collect.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertGreaterEqual(mod.RESPAWN_WAIT, 120.0)

    def test_teleport_speed_is_the_safe_one(self):
        import importlib.util

        from logic.delta_teleport import SAFE_SPEED

        spec = importlib.util.spec_from_file_location("run_collect", "run_collect.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.TP_SPEED, SAFE_SPEED)

    def test_travel_is_not_armed_by_default(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_collect", "run_collect.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # make_travel не шлёт ничего, пока его не позовут — проверяем,
        # что он возвращает функцию и не падает на пустом боте.
        called = []
        fn = mod.make_travel(lambda m: called.append(m))
        self.assertTrue(callable(fn))
        self.assertEqual(called, [])


class SpawnAcceptTests(unittest.TestCase):
    """Свой спавн или чужой — от этого зависит перезаход."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_collect", "run_collect.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_work_ground_is_accepted(self):
        ok, why = self.mod.acceptable_spawn(cr.WORK_POINTS[0])
        self.assertTrue(ok)
        self.assertIn("площадк", why)

    def test_recorded_start_position_is_accepted(self):
        ok, _ = self.mod.acceptable_spawn((-1444.90, 2130.31, 9.62))
        self.assertTrue(ok)

    def test_any_station_is_accepted(self):
        # Вокзал сервер выдаёт случайно — годится любой, везём телепортом.
        from logic.spawn import SPAWNS

        for name, coords in SPAWNS.items():
            if name in self.mod.DENIED_STATIONS:
                continue
            ok, why = self.mod.acceptable_spawn(coords)
            self.assertTrue(ok, f"{name}: {why}")

    def test_southern_is_rejected(self):
        from logic.spawn import SPAWNS

        ok, why = self.mod.acceptable_spawn(SPAWNS["Южный"])
        self.assertFalse(ok)
        self.assertIn("Южный", why)

    def test_auth_zone_is_rejected(self):
        ok, why = self.mod.acceptable_spawn((13.7, 485.3, -20.0))
        self.assertFalse(ok)
        self.assertIn("не в мире", why)

    def test_sky_is_rejected(self):
        ok, _ = self.mod.acceptable_spawn((-1444.9, 2130.3, 2501.0))
        self.assertFalse(ok)

    def test_wrong_spawn_wait_is_one_minute(self):
        self.assertEqual(self.mod.WRONG_SPAWN_WAIT, 60.0)

    def test_walk_travel_uses_move_to_coord(self):
        calls = []

        class Api:
            def getbotposition(self):
                return (-1400.0, 2100.0, 9.6)

            def MoveToCoord(self, x, y, z, **kw):
                calls.append((x, y, z, kw))

        class Bot:
            api = Api()

        said = []
        fn = self.mod.make_walk_travel(said.append)
        fn(Bot(), (-1460.77, 2140.20, 12.60))
        self.assertEqual(len(calls), 1)
        x, y, z, kw = calls[0]
        self.assertAlmostEqual(x, -1460.77)
        self.assertAlmostEqual(y, 2140.20)
        self.assertEqual(kw.get("mode"), "walk")
        # В логе должна быть честная оценка времени, а не бодрый рапорт.
        self.assertTrue(any("пешком" in m for m in said), said)

    def test_banned_flag_is_seen(self):
        class B:
            is_banned = True
            status = "В игре"

        self.assertTrue(self.mod.is_banned(B()))

    def test_ban_in_status_is_seen(self):
        class B:
            is_banned = False
            status = "ЗАБАНЕН (IP)"

        self.assertTrue(self.mod.is_banned(B()))

    def test_normal_status_is_not_a_ban(self):
        class B:
            is_banned = False
            status = "В игре"

        self.assertFalse(self.mod.is_banned(B()))

    def test_missing_fields_do_not_count_as_ban(self):
        class B:
            pass

        self.assertFalse(self.mod.is_banned(B()))

    def test_reentry_is_bounded(self):
        # Без предела цикл перезаходов жжёт аккаунт до бана.
        self.assertGreaterEqual(self.mod.MAX_REENTRY, 1)
        self.assertLessEqual(self.mod.MAX_REENTRY, 20)

    def test_reenter_stops_then_starts_after_wait(self):
        events = []
        slept = []

        class FakeBot:
            status = "В игре"
            proxy = "socks5://213.142.159.108:11080"
            proxy_node = ""

            def stop(self):
                events.append("stop")

            def start(self):
                events.append("start")
                return True

        bot = FakeBot()
        real_sleep = self.mod.time.sleep
        real_wait = self.mod.wait_in_game
        self.mod.time.sleep = lambda s: slept.append(s)
        self.mod.wait_in_game = lambda b, **k: True
        try:
            ok = self.mod.reenter(bot, lambda m: None, wait=60.0)
        finally:
            self.mod.time.sleep = real_sleep
            self.mod.wait_in_game = real_wait
        self.assertTrue(ok)
        self.assertEqual(events, ["stop", "start"])
        self.assertIn(60.0, slept)
        # Прокси остаётся: напрямую сервер отвечает IP-баном (кик 999).
        self.assertEqual(bot.proxy, "socks5://213.142.159.108:11080")

    def test_reenter_without_proxy_is_opt_in(self):
        class FakeBot:
            status = "В игре"
            proxy = "socks5://213.142.159.108:11080"
            proxy_node = ""

            def stop(self):
                pass

            def start(self):
                return True

        bot = FakeBot()
        real_sleep = self.mod.time.sleep
        real_wait = self.mod.wait_in_game
        self.mod.time.sleep = lambda s: None
        self.mod.wait_in_game = lambda b, **k: True
        try:
            self.mod.reenter(bot, lambda m: None, wait=0.0, direct=True)
        finally:
            self.mod.time.sleep = real_sleep
            self.mod.wait_in_game = real_wait
        self.assertEqual(bot.proxy, "")

    def test_reenter_reports_failure_when_start_refuses(self):
        class FakeBot:
            status = "В игре"
            proxy = ""
            proxy_node = ""

            def stop(self):
                pass

            def start(self):
                return False

        real_sleep = self.mod.time.sleep
        self.mod.time.sleep = lambda s: None
        try:
            self.assertFalse(self.mod.reenter(FakeBot(), lambda m: None))
        finally:
            self.mod.time.sleep = real_sleep

    def test_reenter_never_touches_the_manager(self):
        # Через менеджер ходить нельзя: он поднимает весь пул аккаунтов.
        # Проверяем КОД, а не докстринг: в докстринге эти слова как раз
        # объясняют, почему так делать нельзя.
        import ast
        import inspect

        fn = ast.parse(inspect.getsource(self.mod.reenter).lstrip()).body[0]
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        self.assertNotIn("manager", names)
        self.assertNotIn("stop_bot", attrs)
        self.assertNotIn("start_bot", attrs)
        self.assertNotIn("manager", inspect.signature(self.mod.reenter).parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
