"""Тесты мини-игр (интерфейс 31). Без сети и без аккаунта."""
import unittest

import logic.minigames as M


class PacketDetectionTests(unittest.TestCase):
    """is_minigame_packet — детектор по настоящему протоколу.

    Раньше задачи искали ключи "slider", "green_zone", "cursor",
    "target_zone", "target" и `t in (1,2) and "gz" in data`. Ни одного
    из них в протоколе нет, поэтому на реальной игре детектор молчал.
    """

    def test_real_packet(self):
        self.assertTrue(M.is_minigame_packet(31, {"g": 2}))

    def test_all_modes(self):
        for g in (0, 1, 2, 3):
            self.assertTrue(M.is_minigame_packet(31, {"g": g}))

    def test_wrong_interface(self):
        self.assertFalse(M.is_minigame_packet(26, {"g": 2}))
        self.assertFalse(M.is_minigame_packet(63, {"g": 2}))

    def test_no_mode_key(self):
        self.assertFalse(M.is_minigame_packet(31, {"x": 1}))

    def test_fake_keys_rejected(self):
        # Именно эти ключи искал старый мёртвый код.
        for fake in ("slider", "green_zone", "cursor", "target_zone", "target"):
            self.assertFalse(M.is_minigame_packet(31, {fake: 1}))

    def test_gz_rejected(self):
        self.assertFalse(M.is_minigame_packet(31, {"t": 1, "gz": 50}))

    def test_non_dict(self):
        self.assertFalse(M.is_minigame_packet(31, None))
        self.assertFalse(M.is_minigame_packet(31, "строка"))

    def test_string_g(self):
        self.assertTrue(M.is_minigame_packet(31, {"g": "2"}))

    def test_bad_interface(self):
        self.assertFalse(M.is_minigame_packet("нет", {"g": 2}))


class FakeSession:
    connected = True

    def __init__(self):
        self.sent = []

    def send_json(self, iid, data, **kw):
        self.sent.append((iid, data))
        return True


class FakeBot:
    def __init__(self, session=None):
        self.session = session or FakeSession()
        self.logs = []

    def add_log(self, text):
        self.logs.append(str(text))


class ProtocolTests(unittest.TestCase):
    def test_interface_is_31(self):
        self.assertEqual(M.MINIGAME_INTERFACE, 31)

    def test_answer_values(self):
        self.assertEqual(M.ANSWER_HIT, {"s": 1})
        self.assertEqual(M.ANSWER_MISS, {"s": 0})
        self.assertEqual(M.ANSWER_TIMEOUT, {"t": 1})
        self.assertEqual(M.ANSWER_CLOSE, {"c": 1})

    def test_mode_indices_match_layout_order(self):
        # show() добавляет лейауты по индексу: 0 Prize, 1 UnTie, 2 Finger, 3 Image
        self.assertEqual(M.MODE_PRIZE, 0)
        self.assertEqual(M.MODE_UNTIE, 1)
        self.assertEqual(M.MODE_FINGER, 2)
        self.assertEqual(M.MODE_IMAGE, 3)

    def test_parse_mode(self):
        self.assertEqual(M.parse_mode({"g": 2}), 2)
        self.assertEqual(M.parse_mode({"g": "3"}), 3)
        self.assertEqual(M.parse_mode({"g": 2.0}), 2)

    def test_parse_mode_missing(self):
        for bad in ({}, {"s": 1}, None, "мусор", {"g": "abc"}):
            self.assertIsNone(M.parse_mode(bad), bad)

    def test_finger_answers_hit(self):
        self.assertEqual(M.answer_for_mode(M.MODE_FINGER), {"s": 1})

    def test_other_modes_close(self):
        for mode in (M.MODE_PRIZE, M.MODE_UNTIE, M.MODE_IMAGE):
            self.assertEqual(M.answer_for_mode(mode), {"c": 1})


class SolverTests(unittest.TestCase):
    def _solver(self):
        return M.MinigameSolver(delay=(0.0, 0.0))

    def test_finger_sends_hit_to_31(self):
        # Полоса «попади в зону» приходит с геометрией (q*, z, m).
        bot = FakeBot()
        rep = self._solver().handle(bot, {"g": 2, "q1": 0.2, "q2": 0.3, "m": 7})
        self.assertTrue(rep["sent"], rep)
        self.assertEqual(bot.session.sent, [(31, {"s": 1})])

    def test_bare_g2_is_progress_and_answered_not(self):
        # В эталоне (pcap 06.11, 713.97) между двумя ползунками приходит
        # голый {"g":2} — клиент на него НЕ отвечает. Раньше мы отвечали
        # {"s":1} и сбивали добычу руды.
        bot = FakeBot()
        rep = self._solver().handle(bot, {"o": 1, "g": 2})
        self.assertFalse(rep["sent"])
        self.assertEqual(bot.session.sent, [])

    def test_slider_sends_timeout_then_hit(self):
        # Ползунок: {"t":1} и сразу {"s":1}, обе пачки в интерфейс 31.
        bot = FakeBot()
        rep = self._solver().handle(bot, {
            "g": 1, "o": 1, "p_max": 50, "p_max_bar": 100, "p_start": 0,
            "p_tick": 1, "p_timer": 180, "p_title": "Раскопка руды", "x": 1,
        })
        self.assertTrue(rep["sent"], rep)
        self.assertEqual(bot.session.sent, [(31, {"t": 1}), (31, {"s": 1})])

    def test_slider_two_phases_both_answered(self):
        # Второй экран приходит с p_start=50 и должен получить свой ответ.
        bot = FakeBot()
        s = self._solver()
        base = {"g": 1, "o": 1, "p_max_bar": 100, "p_tick": 1,
                "p_timer": 180, "p_title": "Раскопка руды", "x": 1}
        s.handle(bot, dict(base, p_max=50, p_start=0))
        s.handle(bot, dict(base, p_max=100, p_start=50))
        self.assertEqual(len(bot.session.sent), 4)

    def test_prize_sends_close(self):
        bot = FakeBot()
        self._solver().handle(bot, {"g": 0})
        self.assertEqual(bot.session.sent, [(31, {"c": 1})])

    def test_no_g_sends_nothing(self):
        bot = FakeBot()
        rep = self._solver().handle(bot, {"s": 1})
        self.assertFalse(rep["sent"])
        self.assertEqual(bot.session.sent, [])

    def test_same_screen_not_answered_twice(self):
        bot = FakeBot()
        s = self._solver()
        s.handle(bot, {"g": 2, "p_title": "Кресты", "q1": 1.0})
        rep = s.handle(bot, {"g": 2, "p_title": "Кресты", "q1": 1.0})
        self.assertFalse(rep["sent"])
        self.assertEqual(len(bot.session.sent), 1)

    def test_different_screens_both_answered(self):
        bot = FakeBot()
        s = self._solver()
        s.handle(bot, {"g": 2, "p_title": "A", "q1": 1.0})
        s.handle(bot, {"g": 2, "p_title": "B", "q1": 2.0})
        self.assertEqual(len(bot.session.sent), 2)

    def test_dry_run_sends_nothing(self):
        bot = FakeBot()
        rep = self._solver().handle(bot, {"g": 2, "q1": 1.0}, dry_run=True)
        self.assertEqual(rep["answer"], {"s": 1})
        self.assertEqual(bot.session.sent, [])

    def test_dead_session(self):
        bot = FakeBot(FakeSession())
        bot.session.connected = False
        rep = self._solver().handle(bot, {"g": 2, "q1": 1.0})
        self.assertFalse(rep["sent"])
        self.assertIn("сессия", rep["error"])


class HandleTests(unittest.TestCase):
    def test_not_minigame(self):
        bot = FakeBot()
        self.assertFalse(M.handle_minigame(bot, 13, {"g": 2}, background=False))
        self.assertEqual(bot.session.sent, [])

    def test_minigame_true(self):
        bot = FakeBot()
        self.assertTrue(M.handle_minigame(bot, 31, {"g": 2}, background=False))

    def test_bad_interface_id(self):
        self.assertFalse(M.handle_minigame(FakeBot(), None, {}, background=False))
        self.assertFalse(M.handle_minigame(FakeBot(), "x", {}, background=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
