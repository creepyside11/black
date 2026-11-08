"""Тесты аренды скутера и езды — на поддельном боте, без сети."""
import math
import unittest

from logic import scooter


class FakeSender:
    def __init__(self):
        self.packets = []

    def send(self, packet, **kwargs):
        self.packets.append(kwargs.get("packet_name", "?"))
        return True


class FakeSync:
    def __init__(self):
        self.enabled = True
        self.rate_vehicle_min = 0.8
        self.rate_vehicle_max = 1.2
        self.rate_player_min = 0.8
        self.rate_player_max = 1.2
        self._next_player_sync_time = 0.0

    def stop(self):
        self.enabled = False

    def start(self):
        self.enabled = True


class FakeSession:
    def __init__(self, pickups=None):
        self.connected = True
        self.sent = []
        self._sender = FakeSender()
        self.sync = FakeSync()
        self.stream = type("S", (), {"get_all_pickups": lambda _s: pickups or {}})()

    def send_dialog_response(self, dialog_id, response, list_item, text):
        self.sent.append((dialog_id, response, list_item, text))
        return True


class FakeController:
    """Контроллер-заглушка: сразу считает отрезок пройденным."""

    def __init__(self):
        self.config = {}
        self._active = None
        self.target = None

    def get_active(self, movement_id=None):
        return None

    def advance_movement_step(self):
        return None


class FakePlayer:
    def __init__(self, in_vehicle=False, vid=0):
        self._in = in_vehicle
        self._vid = vid
        self.coord_movement = FakeController()
        self.rotation = 0.0

    def is_in_vehicle(self):
        return self._in

    def get_current_vehicle_id(self):
        return self._vid

    def advance_movement_step(self):
        return None

    def __getattr__(self, name):
        # Билдеру player_sync нужны десятки полей — у заглушки все нулевые.
        if name.startswith("_"):
            raise AttributeError(name)
        return 0


class FakeApi:
    def __init__(self, pos):
        self._pos = pos
        self.calls = []

    def getbotposition(self):
        return self._pos

    def MoveToCoord(self, x, y, z, **kwargs):
        self.calls.append({"target": (x, y, z), **kwargs})
        self._pos = (x, y, z)      # заглушка сразу доезжает
        return 1

    def StopMoveToCoord(self, movement_id=None, **kwargs):
        return True


class FakeBot:
    def __init__(self, pos, pickups=None, in_vehicle=False, vid=0):
        self.session = FakeSession(pickups)
        self.session.players = type("P", (), {"bot": FakePlayer(in_vehicle, vid)})()
        self.api = FakeApi(pos)


def pickup(pid, model, x, y, z):
    return {pid: {"pickup_id": pid, "model_id": model, "x": x, "y": y, "z": z}}


class TestPlain(unittest.TestCase):
    def test_strips_samp_colors(self):
        self.assertEqual(
            scooter._plain("{E0584B}BLACK RUSSIA {FFFFFF}| Аренда"),
            "BLACK RUSSIA | Аренда",
        )

    def test_leaves_plain_text(self):
        self.assertEqual(scooter._plain("Аренда"), "Аренда")


class TestFindPickup(unittest.TestCase):
    def test_finds_rental_by_model(self):
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 1, 1, 1))
        found = scooter.find_rental_pickup(bot, (0, 0, 0))
        self.assertEqual(found[0], 2049)

    def test_ignores_other_models(self):
        bot = FakeBot((0, 0, 0), pickup(2048, 1318, 1, 1, 1))
        self.assertIsNone(scooter.find_rental_pickup(bot, (0, 0, 0)))

    def test_ignores_far_pickup(self):
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 50, 50, 50))
        self.assertIsNone(scooter.find_rental_pickup(bot, (0, 0, 0)))

    def test_picks_nearest(self):
        picks = {}
        picks.update(pickup(1, 19131, 5, 0, 0))
        picks.update(pickup(2, 19131, 1, 0, 0))
        bot = FakeBot((0, 0, 0), picks)
        self.assertEqual(scooter.find_rental_pickup(bot, (0, 0, 0))[0], 2)


class TestPickRow(unittest.TestCase):
    def test_finds_scooter_row(self):
        entry = {
            "style": 5,
            "info": "##\tНазвание\tНомер\tДействие\n1.\tСкутер\t[------]\tАрендовать\nДалее",
        }
        self.assertEqual(scooter._pick_row(entry, "скутер"), 0)

    def test_missing_row(self):
        entry = {"style": 5, "info": "1.\tМопед\t[------]\tАрендовать"}
        self.assertIsNone(scooter._pick_row(entry, "скутер"))


class TestRentFlow(unittest.TestCase):
    def test_no_teleport_refuses(self):
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 0, 0, 0))
        res = scooter.rent_scooter(bot, (10, 10, 10), teleport=None, log=lambda *_: None)
        self.assertFalse(res["rented"])
        self.assertIn("телепорт", res["reason"])

    def test_no_pickup_refuses(self):
        bot = FakeBot((0, 0, 0))
        res = scooter.rent_scooter(
            bot, (10, 10, 10), teleport=lambda *_: None, log=lambda *_: None,
            menu_timeout=0.2,
        )
        self.assertFalse(res["rented"])
        self.assertIn("не найден", res["reason"])

    def test_menu_never_opens(self):
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 0, 0, 0))
        res = scooter.rent_scooter(
            bot, (10, 10, 10), teleport=lambda *_: None, dialogs=[],
            log=lambda *_: None, menu_timeout=0.3,
        )
        self.assertFalse(res["rented"])
        self.assertIn("не открылось", res["reason"])

    def test_menu_without_scooter(self):
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 0, 0, 0))
        dialogs = [{
            "dialog_id": 0, "title": "BLACK RUSSIA | Управление транспортом",
            "style": 5, "info": "1.\tМопед\t[------]\tАрендовать",
        }]
        res = scooter.rent_scooter(
            bot, (10, 10, 10), teleport=lambda *_: None, dialogs=dialogs,
            log=lambda *_: None, menu_timeout=0.3,
        )
        self.assertFalse(res["rented"])
        self.assertIn("нет скутера", res["reason"])

    def test_full_flow_teleports_immediately(self):
        """Ответ "Да" и телепорт — без задержки между ними."""
        bot = FakeBot((0, 0, 0), pickup(2049, 19131, 0, 0, 0), in_vehicle=True, vid=293)
        menu = {
            "dialog_id": 0, "title": "BLACK RUSSIA | Управление транспортом",
            "style": 5,
            "info": "##\tНазвание\tНомер\tДействие\n1.\tСкутер\t[------]\tАрендовать",
        }
        confirm = {
            "dialog_id": 0, "title": "BLACK RUSSIA | Аренда", "style": 0,
            "info": "Арендовать Скутер за 100 рублей?",
        }
        dialogs = [menu]
        teleported = []
        order = []

        real_send = bot.session.send_dialog_response

        def send(dialog_id, response, list_item, text):
            ok = real_send(dialog_id, response, list_item, text)
            order.append("answer")
            if len(bot.session.sent) == 1:
                dialogs.append(confirm)   # сервер шлёт окно в ответ на выбор
            return ok

        bot.session.send_dialog_response = send

        def fake_teleport(b, point):
            order.append("teleport")
            teleported.append(point)
            return {"status": "ok"}

        res = scooter.rent_scooter(
            bot, (10, 20, 30), teleport=fake_teleport, dialogs=dialogs,
            log=lambda *_: None,
        )
        self.assertTrue(res["rented"])
        self.assertEqual(teleported, [(10, 20, 30)])
        self.assertEqual(res["vehicle_id"], 293)
        self.assertEqual(len(bot.session.sent), 2)
        self.assertEqual(bot.session.sent[1][1], 1)
        # телепорт сразу после ответа "Да", без промежуточных шагов
        self.assertEqual(order, ["answer", "answer", "teleport"])


class TestDrive(unittest.TestCase):
    def test_no_position(self):
        bot = FakeBot(None)
        self.assertEqual(scooter.drive_to(bot, (1, 2, 3))["status"], "no_position")

    def test_start_failure_reports(self):
        bot = FakeBot((0, 0, 0))

        def boom(*a, **k):
            raise RuntimeError("nope")

        bot.api.MoveToCoord = boom
        res = scooter.drive_to(bot, (1, 2, 3), log=lambda *_: None)
        self.assertEqual(res["status"], "error")


class TestSteering(unittest.TestCase):
    """Поворот — руль по пройденному пути, а не вращение корпуса на месте."""

    def _controller(self):
        from logic.movement import CoordMovementController

        player = FakePlayer(in_vehicle=True, vid=293)
        player.x, player.y, player.z = 0.0, 0.0, 10.0
        player.rotation = 0.0
        player.set_rotation = lambda r: setattr(player, "rotation", float(r) % 360.0)
        player._set_position_internal = lambda x, y, z, **k: (
            setattr(player, "x", x), setattr(player, "y", y), setattr(player, "z", z)
        )
        player.get_position = lambda: (player.x, player.y, player.z)
        player.keys = 0
        player.lr = 0
        player.ud = 0
        player.set_control_axes = lambda lr, ud: (setattr(player, "lr", lr), setattr(player, "ud", ud))
        player.reset_velocity = lambda: None
        player.update_movement_velocity = lambda *a, **k: None
        player.set_coord_delay = lambda *a, **k: None
        player.clear_position_target = lambda: None
        player.session = type("S", (), {"stream": None})()
        return CoordMovementController(player)

    def test_standstill_does_not_spin(self):
        ctrl = self._controller()
        state, _ = ctrl.start(
            (0.0, 0.0, 10.0), mode="vehicle",
            overrides={"movement_direction_control": (True, True, 0, 45)},
        )
        before = ctrl.player.rotation
        ctrl.step()
        self.assertAlmostEqual(ctrl.player.rotation, before, places=3)

    def test_turn_is_bounded_by_radius(self):
        ctrl = self._controller()
        ctrl.start(
            (0.0, -50.0, 10.0), mode="vehicle",
            overrides={
                "movement_direction_control": (True, True, 0, 100),
                "coord_delay": 0.4,
                "turn_radius": 4.5,
            },
        )
        ctrl.step()
        # за один шаг (0.4м) при радиусе 4.5м — не больше ~5.1°
        turn = abs((ctrl.player.rotation + 180.0) % 360.0 - 180.0)
        self.assertLessEqual(turn, 6.0)

    def _sim(self, target, steps=80, dt=1.0 / 30.0):
        ctrl = self._controller()
        ctrl.start(
            target, mode="vehicle",
            overrides={
                "coord_delay": 0.4,
                "completion_distance": 0.5,
                "turn_radius": 3.0,
                "max_steer": 35.0,
                "movement_direction_control": (True, True, 0, 45),
                "update_lr_ud": True,
            },
        )
        track = []
        t = 0.0
        for _ in range(steps):
            t += dt
            ctrl.step(t)
            track.append(
                (ctrl.player.x, ctrl.player.y, ctrl.player.z, ctrl.player.rotation)
            )
        return ctrl, track

    def test_vehicle_moves_only_along_heading(self):
        import math as _m

        ctrl, track = self._sim((0.0, -100.0, 10.0))
        prev = (0.0, 0.0)
        checked = 0
        for x, y, z, rot in track:
            dx, dy = x - prev[0], y - prev[1]
            if abs(dx) > 1e-9 or abs(dy) > 1e-9:
                h = _m.radians(rot)
                ex, ey = -_m.sin(h), _m.cos(h)
                # поперёк курса — ноль, вдоль курса — вперёд
                self.assertAlmostEqual(dx * ey - dy * ex, 0.0, places=6)
                self.assertGreater(dx * ex + dy * ey, 0.0)
                checked += 1
            prev = (x, y)
        self.assertGreater(checked, 10)

    def test_vehicle_z_never_changes(self):
        ctrl, track = self._sim((0.0, -100.0, 25.0))
        for _x, _y, z, _rot in track:
            self.assertAlmostEqual(z, 10.0, places=6)

    def test_vehicle_does_not_spin_while_stopped(self):
        ctrl, track = self._sim((0.0, 0.4, 10.0), steps=20)
        for _x, _y, _z, rot in track:
            self.assertAlmostEqual(rot, 0.0, places=3)

    def test_vehicle_turns_gradually(self):
        ctrl, track = self._sim((0.0, -100.0, 10.0))
        prev = 0.0
        for _x, _y, _z, rot in track:
            delta = abs((rot - prev + 180.0) % 360.0 - 180.0)
            self.assertLessEqual(delta, 12.0)
            prev = rot

    def test_vehicle_keeps_ground_z(self):
        ctrl = self._controller()
        ctrl.start(
            (0.0, -50.0, 25.0), mode="vehicle",   # цель высоко над землёй
            overrides={"coord_delay": 0.4},
        )
        ctrl.step()
        self.assertAlmostEqual(ctrl.player.z, 10.0, places=3)


class TestExitVehicle(unittest.TestCase):
    """Сход с транспорта — только через setbotvehicle(0)."""

    def test_exit_uses_setbotvehicle_zero(self):
        bot = FakeBot((0, 0, 0), in_vehicle=True, vid=339)
        calls = []

        def setbotvehicle(vid, seat=0):
            calls.append((vid, seat))
            bot.session.players.bot._vid = 0
            return True

        bot.api.setbotvehicle = setbotvehicle
        res = scooter.exit_vehicle(bot)
        self.assertEqual(calls, [(0, 0)])
        self.assertTrue(res["exited"])
        self.assertEqual(res["previous_vehicle_id"], 339)
        self.assertEqual(res["vehicle_id"], 0)

    def test_exit_when_already_on_foot(self):
        bot = FakeBot((0, 0, 0), in_vehicle=False, vid=0)
        bot.api.setbotvehicle = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("не должен звать setbotvehicle")
        )
        res = scooter.exit_vehicle(bot)
        self.assertFalse(res["exited"])
        self.assertIn("пешком", res["reason"])

    def test_exit_reports_failure(self):
        bot = FakeBot((0, 0, 0), in_vehicle=True, vid=339)
        bot.api.setbotvehicle = lambda *a, **k: False
        res = scooter.exit_vehicle(bot)
        self.assertFalse(res["exited"])
        self.assertEqual(res["vehicle_id"], 339)


class TestPauseWarp(unittest.TestCase):
    """Телепорт в паузе: синк стоп -> позиция -> синк старт."""

    def _bot(self):
        bot = FakeBot((0.0, 0.0, 0.0))
        player = bot.session.players.bot
        player._set_position_internal = lambda x, y, z, **k: setattr(
            bot.api, "_pos", (x, y, z)
        )
        player.clear_position_target = lambda: None
        player.reset_velocity = lambda: None
        return bot

    def test_pause_stops_and_restarts_sync(self):
        bot = self._bot()
        calls = []
        bot.session.sync.stop = lambda: calls.append("stop")
        bot.session.sync.start = lambda: calls.append("start")
        bot.session.sync.enabled = True
        res = scooter.pause_warp(bot, (5, 5, 0), hold=0.0, log=lambda *_: None)
        self.assertEqual(calls, ["stop", "start"])
        self.assertEqual(res["status"], "arrived")

    def test_position_changes_while_paused(self):
        bot = self._bot()
        seen = []
        bot.session.sync.stop = lambda: seen.append(bot.api.getbotposition())
        bot.session.sync.start = lambda: seen.append(bot.api.getbotposition())
        bot.session.sync.enabled = True
        scooter.pause_warp(bot, (7, 9, 0), hold=0.0, log=lambda *_: None)
        # до остановки синка позиция старая, после — уже новая
        self.assertEqual(seen[0], (0.0, 0.0, 0.0))
        self.assertEqual(seen[1], (7.0, 9.0, 0.0))

    def test_disabled_sync_is_not_started(self):
        bot = self._bot()
        bot.session.sync.enabled = False
        calls = []
        bot.session.sync.start = lambda: calls.append("start")
        scooter.pause_warp(bot, (1, 1, 0), hold=0.0, log=lambda *_: None)
        self.assertEqual(calls, [])

    def test_long_distance_is_split_into_chunks(self):
        """Сервер откатывает прыжок больше ~45м, поэтому идём кусками."""
        bot = self._bot()
        bot.session.sync.enabled = True
        bot.session.sync.stop = lambda: None
        bot.session.sync.start = lambda: None
        res = scooter.pause_warp(
            bot, (0, 600, 0), max_step=45.0, hold=0.0, log=lambda *_: None,
        )
        self.assertEqual(res["steps"], 14)      # 600 / 45 -> 14 кусков
        self.assertEqual(res["steps_done"], 14)
        self.assertEqual(res["status"], "arrived")

    def test_rollback_stops_further_chunks(self):
        bot = self._bot()
        bot.session.sync.enabled = True
        bot.session.sync.stop = lambda: None
        bot.session.sync.start = lambda: None

        calls = {"n": 0}
        real = bot.session.players.bot._set_position_internal

        def stubborn(x, y, z, **k):
            calls["n"] += 1
            if calls["n"] > 2:
                return                      # сервер больше не двигает
            real(x, y, z, **k)

        bot.session.players.bot._set_position_internal = stubborn
        res = scooter.pause_warp(
            bot, (0, 200, 0), max_step=45.0, hold=0.0, log=lambda *_: None,
        )
        self.assertEqual(res["status"], "stopped")
        self.assertLess(res["steps_done"], res["steps"])

    def test_reports_distance_left(self):
        bot = self._bot()
        bot.session.sync.enabled = True
        bot.session.sync.stop = lambda: None
        bot.session.sync.start = lambda: None
        res = scooter.pause_warp(bot, (3, 4, 0), hold=0.0, log=lambda *_: None)
        self.assertEqual(res["left"], 0.0)
        self.assertEqual(res["final_position"], (3.0, 4.0, 0.0))


class TestWalk(unittest.TestCase):
    def test_walk_route_uses_walk_mode(self):
        bot = FakeBot((0, 0, 0))
        res = scooter.walk_route(bot, [(5, 5, 0), (10, 10, 0)], log=lambda *_: None)
        self.assertEqual(res["status"], "arrived")
        self.assertEqual(len(res["legs"]), 2)
        for call in bot.api.calls:
            self.assertEqual(call["mode"], "walk")

    def _config_during_call(self, fn):
        """Конфиг контроллера в момент старта отрезка."""
        bot = FakeBot((0, 0, 0))
        seen = []
        real = bot.api.MoveToCoord

        def capture(x, y, z, **kwargs):
            seen.append(dict(bot.session.players.bot.coord_movement.config))
            return real(x, y, z, **kwargs)

        bot.api.MoveToCoord = capture
        fn(bot)
        return seen[0] if seen else {}

    def test_walk_uses_own_turn_radius(self):
        cfg = self._config_during_call(
            lambda b: scooter.walk_route(b, [(5, 5, 0)], log=lambda *_: None)
        )
        self.assertLess(cfg["walk_turn_radius"], 1.0)

    def test_walk_restores_config_after_finish(self):
        bot = FakeBot((0, 0, 0))
        bot.session.players.bot.coord_movement.config["walk_turn_radius"] = 9.9
        scooter.walk_route(bot, [(5, 5, 0)], log=lambda *_: None)
        self.assertEqual(
            bot.session.players.bot.coord_movement.config["walk_turn_radius"], 9.9
        )

    def test_walk_raises_player_sync_rate(self):
        """Пешком шаг считается от rate_player_*, его тоже надо поднимать."""
        bot = FakeBot((0, 0, 0))
        seen = []
        real = bot.api.MoveToCoord

        def capture(x, y, z, **kwargs):
            seen.append(bot.session.sync.rate_player_min)
            return real(x, y, z, **kwargs)

        bot.api.MoveToCoord = capture
        scooter.walk_route(bot, [(5, 5, 0)], log=lambda *_: None)
        self.assertLess(seen[0], 0.1)

    def test_walk_restores_player_sync_rate(self):
        bot = FakeBot((0, 0, 0))
        bot.session.sync.rate_player_min = 0.8
        bot.session.sync.rate_player_max = 1.2
        scooter.walk_route(bot, [(5, 5, 0)], log=lambda *_: None)
        self.assertEqual(bot.session.sync.rate_player_min, 0.8)
        self.assertEqual(bot.session.sync.rate_player_max, 1.2)

    def test_restore_resets_next_sync_time(self):
        """Возврат старого дедлайна синка замораживал бота после маршрута."""
        bot = FakeBot((0, 0, 0))
        bot.session.sync._next_player_sync_time = 12345.0
        scooter.walk_route(bot, [(5, 5, 0)], log=lambda *_: None)
        self.assertEqual(bot.session.sync._next_player_sync_time, 0.0)

    def test_walk_sends_player_sync_packets(self):
        """Пешком нужен свой поток: иначе сервер видит прыжок без анимации."""
        bot = FakeBot((0, 0, 0))
        scooter.walk_to(bot, (5, 5, 0), timeout=2.0, log=lambda *_: None)
        self.assertIn("player_sync", bot.session._sender.packets)

    def test_walk_does_not_send_vehicle_sync(self):
        bot = FakeBot((0, 0, 0))
        scooter.walk_to(bot, (5, 5, 0), timeout=2.0, log=lambda *_: None)
        self.assertNotIn("vehicle_sync", bot.session._sender.packets)

    def test_walk_route_empty(self):
        bot = FakeBot((0, 0, 0))
        self.assertEqual(
            scooter.walk_route(bot, [], log=lambda *_: None)["status"],
            "empty_route",
        )


class TestRoute(unittest.TestCase):
    def test_route_runs_every_leg_in_one_session(self):
        bot = FakeBot((0, 0, 0))
        points = [(10, 0, 0), (10, 10, 0)]
        res = scooter.drive_route(bot, points, log=lambda *_: None)
        self.assertEqual(res["status"], "arrived")
        self.assertEqual(len(res["legs"]), 2)
        # оба отрезка прошли через один и тот же контроллер, без перезапуска
        self.assertEqual(len(bot.api.calls), 2)

    def test_route_uses_smooth_turn(self):
        bot = FakeBot((0, 0, 0))
        scooter.drive_route(bot, [(5, 5, 0)], log=lambda *_: None)
        control = bot.api.calls[0]["movement_direction_control"]
        self.assertEqual(len(control), 4)
        self.assertLess(control[3], 100.0)

    def test_route_empty(self):
        bot = FakeBot((0, 0, 0))
        self.assertEqual(
            scooter.drive_route(bot, [], log=lambda *_: None)["status"],
            "empty_route",
        )

    def test_sync_restored_after_route(self):
        bot = FakeBot((0, 0, 0))
        bot.session.sync.enabled = True
        scooter.drive_route(bot, [(1, 1, 0)], log=lambda *_: None)
        self.assertTrue(bot.session.sync.enabled)

    def test_settings_restored_after_route(self):
        bot = FakeBot((0, 0, 0))
        bot.session.players.bot.coord_movement.config["avoid_obstacles"] = True
        scooter.drive_route(bot, [(1, 1, 0)], log=lambda *_: None)
        self.assertTrue(
            bot.session.players.bot.coord_movement.config["avoid_obstacles"]
        )


if __name__ == "__main__":
    unittest.main()
