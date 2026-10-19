"""Статус спавна: бот, который уже стоит на вокзале, должен получить «заспавнен».

Раньше begin_spawn_watchdog выходил рано, не вызвав mark_spawned, и такой бот
навсегда оставался «В игре (авторизован)» — при том что физически стоял на
вокзале с игроками вокруг. Всё, что фильтрует по статусу, его не видело.
"""

import unittest
from unittest.mock import MagicMock, patch

import bot as botmod
from logic.spawn import is_spawned_position

Yuzhny = (2742.063, -2445.180, 21.813)
AuthZone = (13.703383445739746, 485.29071044921875, -20.0)


class AlreadyOnStationTests(unittest.TestCase):
    def _bot(self, position):
        b = MagicMock()
        b.stopping = False
        b.is_banned = False
        b.spawn_watchdog = None
        b.status = "В игре (авторизован)"
        b.api.getbotposition.return_value = position
        b.add_log = lambda *a, **k: None
        # mark_spawned оставляем настоящим — проверяем именно его вызов
        b.mark_spawned = botmod.BotInstance.mark_spawned.__get__(b, MagicMock)
        return b

    def test_position_is_real_spawn(self):
        self.assertTrue(is_spawned_position(Yuzhny))
        self.assertFalse(is_spawned_position(AuthZone))

    def test_marks_spawned_when_already_on_station(self):
        b = self._bot(Yuzhny)
        with patch("logic.spawn.SpawnWatchdog") as wd:
            botmod.BotInstance.begin_spawn_watchdog(b)
            wd.assert_not_called()
        self.assertEqual(b.status, "В игре (заспавнен)")

    def test_watchdog_starts_when_in_auth_zone(self):
        b = self._bot(AuthZone)
        with patch("logic.spawn.SpawnWatchdog") as wd:
            botmod.BotInstance.begin_spawn_watchdog(b)
            wd.assert_called_once()
        self.assertEqual(b.status, "В игре (авторизован)")

    def test_no_double_watchdog_when_already_running(self):
        b = self._bot(AuthZone)
        b.spawn_watchdog = MagicMock(running=True)
        with patch("logic.spawn.SpawnWatchdog") as wd:
            botmod.BotInstance.begin_spawn_watchdog(b)
            wd.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
