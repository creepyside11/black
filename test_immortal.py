import time
import unittest

from logic import immortal


class FakePlayer:
    def __init__(self, health=0.0):
        self.health = health
        self.godmode_zero_xp = False


class FakeBot:
    def __init__(self, health=0.0):
        self.session = type("S", (), {})()
        self.session.players = type("P", (), {})()
        self.session.players.bot = FakePlayer(health)


class TestApply(unittest.TestCase):
    def test_apply_raises_health_and_godmode(self):
        bot = FakeBot(12.0)
        self.assertTrue(immortal.apply(bot))
        self.assertEqual(bot.session.players.bot.health, 100.0)
        self.assertTrue(bot.session.players.bot.godmode_zero_xp)

    def test_apply_without_session(self):
        bot = type("B", (), {})()
        self.assertFalse(immortal.apply(bot))

    def test_disable_clears_flag(self):
        bot = FakeBot()
        immortal.apply(bot)
        immortal.disable(bot)
        self.assertFalse(bot.session.players.bot.godmode_zero_xp)


class TestGuard(unittest.TestCase):
    def test_guard_keeps_health_up(self):
        bot = FakeBot(5.0)
        guard = immortal.ImmortalGuard(bot, tick=0.02, log=lambda *_: None).start()
        try:
            bot.session.players.bot.health = 3.0      # голод снял HP
            time.sleep(0.12)
            self.assertEqual(bot.session.players.bot.health, 100.0)
            self.assertGreater(guard.applied, 1)
        finally:
            guard.stop()

    def test_guard_stop_turns_godmode_off(self):
        bot = FakeBot()
        guard = immortal.ImmortalGuard(bot, tick=0.02, log=lambda *_: None).start()
        time.sleep(0.06)
        guard.stop()
        self.assertFalse(bot.session.players.bot.godmode_zero_xp)
        self.assertFalse(guard.running)

    def test_enable_is_idempotent(self):
        immortal.stop(log=lambda *_: None)
        bot = FakeBot()
        first = immortal.enable(bot, log=lambda *_: None)
        second = immortal.enable(bot, log=lambda *_: None)
        try:
            self.assertIs(first, second)
            self.assertEqual(immortal.status(bot)["running"], True)
        finally:
            immortal.stop(log=lambda *_: None)

    def test_status_reports_state(self):
        immortal.stop(log=lambda *_: None)
        bot = FakeBot()
        self.assertFalse(immortal.status(bot)["running"])
        guard = immortal.enable(bot, log=lambda *_: None)
        try:
            st = immortal.status(bot)
            self.assertTrue(st["running"])
            self.assertEqual(st["health"], 100.0)
            self.assertTrue(st["godmode"])
        finally:
            immortal.stop(log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
