"""Один ник — одна запись в менеджере.

Живой случай: повторные add_bot наплодили восемь записей Wendel_Nezovibatko
в accounts.json. Все восемь потом по очереди лезли в один аккаунт и выбивали
друг друга киком «уже в сети» (код 2).
"""

import os
import tempfile
import unittest
from unittest import mock

import bot as botmod


class DuplicateTests(unittest.TestCase):
    """Тесты не должны трогать настоящий accounts.json.

    add_bot сохраняет состояние на диск, поэтому первый заход этих тестов
    дописал в боевой файл четыре тестовых аккаунта. Подменяем путь на
    временный.
    """

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self._patch = mock.patch.object(botmod, "ACCOUNTS_FILE", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_same_nick_not_added_twice(self):
        m = botmod.BotManager()
        first = m.add_bot("TOLYATTI", "Dubl_Test", "55337q", default_task="stop")
        self.assertIsNotNone(first)
        count_before = len(m.bots)
        second = m.add_bot("TOLYATTI", "Dubl_Test", "55337q", default_task="stop")
        self.assertIsNotNone(second)
        self.assertEqual(len(m.bots), count_before, "дубль добавился")
        self.assertIs(second, first, "должна вернуться существующая запись")

    def test_case_insensitive(self):
        m = botmod.BotManager()
        m.add_bot("TOLYATTI", "Registr_Test", "55337q", default_task="stop")
        count = len(m.bots)
        m.add_bot("TOLYATTI", "registr_test", "55337q", default_task="stop")
        self.assertEqual(len(m.bots), count)

    def test_different_nicks_both_added(self):
        m = botmod.BotManager()
        m.add_bot("TOLYATTI", "Pervyj_Test", "55337q", default_task="stop")
        m.add_bot("TOLYATTI", "Vtoroj_Test", "55337q", default_task="stop")
        nicks = {b.nickname for b in m.bots.values()}
        self.assertIn("Pervyj_Test", nicks)
        self.assertIn("Vtoroj_Test", nicks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
