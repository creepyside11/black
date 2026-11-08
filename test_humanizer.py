"""Живое поведение: скорость ходьбы, повороты, паузы, перерывы."""

import math
import random
import unittest

from logic.humanizer import (
    Humanizer,
    coord_delay_for_speed,
    WALK_SPEED,
    JOG_SPEED,
    SESSION_HOURS,
)
from logic.movement import MAX_STEP_DT


class FakePlayer:
    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.keys = 0
        self.lr = 0
        self.ud = 0
        self.rotation = 0.0
        self.vehicle_id = -1

    def get_position(self):
        return (self.x, self.y, self.z)

    def is_in_vehicle(self):
        return False

    def set_coord_delay(self, _value):
        pass

    def set_rotation(self, value):
        self.rotation = value

    def set_control_axes(self, _a, _b):
        pass

    def _set_position_internal(self, x, y, z, update_velocity=False, now=None):
        self.x, self.y, self.z = x, y, z

    def update_movement_velocity(self, *a, **k):
        pass

    def reset_velocity(self):
        pass

    def clear_position_target(self):
        pass


class FakeBot:
    def __init__(self):
        self.running = True
        self.logs = []

    def add_log(self, message):
        self.logs.append(message)


class FakeSync:
    def __init__(self, interval=0.8):
        self.rate_player_min = interval
        self.rate_player_max = interval


class FakeSession:
    def __init__(self, interval=0.8):
        self.sync = FakeSync(interval)


class FakeApi:
    """Пишет вызовы и умеет довести бота до цели без реальной сети."""

    def __init__(self, player, sync_interval=0.8):
        self.player = player
        self.sync_interval = sync_interval
        self.calls = []
        self.rotation_set = []
        self.stopped = 0
        self.keys_sent = []

    def getbotposition(self):
        return self.player.get_position()

    def setbotrotation(self, angle):
        self.rotation_set.append(angle)
        self.player.rotation = angle

    def MoveToCoord(self, x, y, z, **kwargs):
        self.calls.append({"x": x, "y": y, "z": z, **kwargs})
        # Мгновенно телепортируем в тесте: проверяем не физику, а аргументы.
        self.player.x, self.player.y, self.player.z = x, y, z

    def StopMoveToCoord(self, reason="cancelled"):
        self.stopped += 1

    def SetKey(self, keys=None, lr=None, ud=None):
        self.keys_sent.append(keys)
        return True


class CoordDelayTests(unittest.TestCase):
    def test_formula_matches_controller_step(self):
        """coord_delay_for_speed должен давать ту скорость, что посчитает контроллер."""
        for sync in (0.2, 0.5, 0.8, 1.5):
            for speed in (1.0, 1.2, 2.0):
                cd = coord_delay_for_speed(speed, sync)
                step = min(sync, MAX_STEP_DT)
                actual = cd * step / (sync * sync)
                self.assertAlmostEqual(actual, speed, places=4,
                                       msg=f"sync={sync} speed={speed}")

    def test_eco_mode_needs_much_bigger_delay(self):
        """В ECO sync вчетверо реже, значит coord_delay должен быть вчетверо больше."""
        eco = coord_delay_for_speed(1.2, 0.8)
        fast = coord_delay_for_speed(1.2, 0.2)
        self.assertGreater(eco, fast * 10)
        self.assertGreater(eco, 3.0)

    def test_old_statue_value_is_rejected_as_too_slow(self):
        """0.03 — то, из-за чего бот стоял. Скорость обязана быть мизерной."""
        cd = 0.03
        step = min(0.8, MAX_STEP_DT)
        speed = cd * step / (0.8 * 0.8)
        self.assertLess(speed, 0.05, "0.03 не должен давать нормальный ход")

    def test_zero_sync_does_not_crash(self):
        self.assertGreater(coord_delay_for_speed(1.2, 0.0), 0.0)


class WalkTests(unittest.TestCase):
    def _humanizer(self, sync=0.8):
        player = FakePlayer()
        # (0,0) в проекте означает «ещё не заспавнен», поэтому ставим бота
        # в обычную точку карты.
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player, sync)
        bot = FakeBot()
        humanizer = Humanizer(bot, api, FakeSession(sync), log=lambda _m: None)
        return humanizer, api, player, bot

    def test_walk_uses_real_controller_not_tiny_delay(self):
        humanizer, api, player, _ = self._humanizer()
        humanizer.walk_to(1220.0, 1250.0, 20.0)

        self.assertEqual(len(api.calls), 1)
        call = api.calls[0]
        # Главная регрессия: раньше тут стояло 0.025-0.06 и бот не двигался.
        self.assertGreater(call["coord_delay"], 2.0,
                           "coord_delay снова стал крошечным — бот будет стоять")
        self.assertEqual(call["mode"], "walk")

    def test_walk_enables_direction_control(self):
        humanizer, api, _, _ = self._humanizer()
        humanizer.walk_to(1220.0, 1250.0, 20.0)
        control = api.calls[0]["movement_direction_control"]
        face_movement, update_lr_ud, deviation, turn_speed = control
        self.assertTrue(face_movement, "модель должна поворачиваться по курсу")
        self.assertTrue(update_lr_ud, "клиент обновляет lr/ud")
        self.assertLessEqual(deviation, 100)
        self.assertGreater(turn_speed, 0)

    def test_walk_randomizes_speed(self):
        humanizer, api, _, _ = self._humanizer()
        humanizer.walk_to(1220.0, 1250.0, 20.0)
        self.assertIsNotNone(api.calls[0]["randomization"])

    def test_run_holds_forward_key(self):
        humanizer, api, _, _ = self._humanizer()
        humanizer.walk_to(20.0, 0.0, 0.0, run=True)
        self.assertEqual(api.calls[0]["movement_keys_mode"], "throttle:8")

    def test_walk_speed_within_human_range(self):
        humanizer, api, _, _ = self._humanizer()
        humanizer.walk_to(1220.0, 1250.0, 20.0)
        cd = api.calls[0]["coord_delay"]
        actual = cd * min(0.8, MAX_STEP_DT) / (0.8 * 0.8)
        self.assertGreaterEqual(actual, WALK_SPEED[0] - 0.01)
        self.assertLessEqual(actual, WALK_SPEED[1] + 0.01)

    def test_close_target_skips_walking(self):
        humanizer, api, player, _ = self._humanizer()
        player.x, player.y, player.z = 1205.0, 1255.0, 20.0
        self.assertTrue(humanizer.walk_to(1205.2, 1255.2, 20.0))
        self.assertEqual(api.calls, [], "на полметра идти не надо")

    def test_walk_stops_when_bot_stops(self):
        humanizer, api, player, bot = self._humanizer()
        bot.running = False
        humanizer.walk_to(1700.0, 1250.0, 20.0)
        self.assertGreaterEqual(api.stopped, 1, "ходьбу надо останавливать")


class RotationTests(unittest.TestCase):
    def _humanizer(self):
        player = FakePlayer()
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player)
        return Humanizer(FakeBot(), api, FakeSession(), log=lambda _m: None), api, player

    def test_turn_uses_existing_method(self):
        """setbotangle не существует — раньше поворот молча не работал."""
        humanizer, api, _ = self._humanizer()
        humanizer.turn_towards(90.0)
        self.assertTrue(api.rotation_set, "бот не повернулся вообще")
        self.assertAlmostEqual(api.rotation_set[-1], 90.0, places=1)

    def test_turn_is_smooth_not_snap(self):
        humanizer, api, _ = self._humanizer()
        humanizer.turn_towards(180.0)
        self.assertGreaterEqual(len(api.rotation_set), 3,
                                "поворот должен идти несколькими шагами")

    def test_turn_takes_shortest_path(self):
        humanizer, api, player = self._humanizer()
        player.rotation = 350.0
        humanizer.rotation = 350.0
        humanizer.turn_towards(10.0)
        # Кратчайший путь через 0, а не назад через 180.
        self.assertFalse(any(180.0 < a < 350.0 for a in api.rotation_set),
                         f"поворот пошёл длинным путём: {api.rotation_set}")

    def test_rotation_wraps_into_range(self):
        humanizer, api, _ = self._humanizer()
        humanizer.turn_towards(725.0)
        self.assertLess(api.rotation_set[-1], 360.0)
        self.assertGreaterEqual(api.rotation_set[-1], 0.0)


class BehaviourTests(unittest.TestCase):
    def test_step_returns_positive_pause(self):
        """step() возвращает паузу, а не падает.

        Раньше тест требовал, чтобы за один вызов обязательно было
        действие. Это не контракт: ветка «стоит» спит случайные 2-7 сек
        и с вероятностью 65% не поворачивается вовсе — короткая пауза
        законно заканчивается без единого вызова. Тест падал через раз
        на ровном месте.
        """
        player = FakePlayer()
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player)
        humanizer = Humanizer(FakeBot(), api, FakeSession(), log=lambda _m: None)
        pause = humanizer.step()
        self.assertIsInstance(pause, float)

    def test_step_is_alive_over_many_calls(self):
        """А вот за десяток вызовов действие обязано случиться."""
        player = FakePlayer()
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player)
        humanizer = Humanizer(
            FakeBot(), api, FakeSession(), log=lambda _m: None,
            rng=random.Random(7),
        )
        for _ in range(12):
            humanizer.step()
        self.assertGreater(len(api.calls) + len(api.rotation_set), 0,
                           "за 12 вызовов не сделано ни одного действия")

    def test_break_returns_negative(self):
        """Перерыв отдаётся отрицательным числом — вызывающий выходит из игры."""
        player = FakePlayer()
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player)
        humanizer = Humanizer(FakeBot(), api, FakeSession(), log=lambda _m: None)
        humanizer.session_started -= humanizer.session_limit + 1
        pause = humanizer.step()
        self.assertLess(pause, 0)
        self.assertGreater(abs(pause), 60.0)

    def test_new_session_resets_limits(self):
        player = FakePlayer()
        player.x, player.y, player.z = 1200.0, 1250.0, 20.0
        api = FakeApi(player)
        humanizer = Humanizer(FakeBot(), api, FakeSession(), log=lambda _m: None)
        old_limit = humanizer.session_limit
        humanizer.session_limit = 1.0
        humanizer.new_session()
        self.assertGreater(humanizer.session_limit, 1.0)
        self.assertNotEqual(humanizer.session_limit, 1.0)
        self.assertGreaterEqual(humanizer.session_limit,
                                SESSION_HOURS[0] * 3600.0 - 1)

    def test_no_position_does_not_crash(self):
        class DeadApi(FakeApi):
            def getbotposition(self):
                return None

        humanizer = Humanizer(FakeBot(), DeadApi(FakePlayer()), FakeSession(),
                              log=lambda _m: None)
        self.assertGreater(humanizer.step(), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
