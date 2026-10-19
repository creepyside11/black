"""Один ник — одна запись в менеджере.

Живой случай: повторные add_bot наплодили восемь записей Wendel_Nezovibatko
в accounts.json. Все восемь потом по очереди лезли в один аккаунт и выбивали
друг друга киком «уже в сети» (код 2).
"""

import json
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


class LoadTests(unittest.TestCase):
    """Дубли в самом accounts.json тоже не должны становиться ботами."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        payload = [
            {"nickname": "Dubl_Load", "password": "55337q", "server": "TOLYATTI"},
            {"nickname": "Dubl_Load", "password": "55337q", "server": "TOLYATTI"},
            {"nickname": "dubl_load", "password": "55337q", "server": "TOLYATTI"},
            # Битая запись: ключ есть, значение null. Раньше роняла весь файл.
            {"nickname": None, "password": "pass_br_123", "server": "BELGOROD"},
            "не словарь",
            {"nickname": "Odinochnyj_Load", "password": "55337q", "server": "TOLYATTI"},
        ]
        json.dump(payload, self._tmp)
        self._tmp.close()
        self._patch = mock.patch.object(botmod, "ACCOUNTS_FILE", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_duplicates_in_file_loaded_once(self):
        # BotManager сам грузит accounts.json в __init__ — второй вызов
        # добавил бы записи повторно и тест ловил бы сам себя.
        m = botmod.BotManager()
        nicks = [b.nickname for b in m.bots.values()]
        lowered = [n.lower() for n in nicks]
        self.assertEqual(len(lowered), len(set(lowered)), f"дубли загрузились: {nicks}")
        self.assertIn("Odinochnyj_Load", nicks)

    def test_bad_entry_does_not_kill_the_rest(self):
        """Запись с nickname=null не должна съедать весь файл.

        Живой случай: одна такая запись на VPS роняла загрузку accounts.json,
        и всё, что шло после неё, не загружалось вовсе.
        """
        m = botmod.BotManager()
        nicks = [b.nickname for b in m.bots.values()]
        self.assertIn("Dubl_Load", nicks, "записи до битой не загрузились")
        self.assertIn("Odinochnyj_Load", nicks, "записи после битой не загрузились")


class SaveTests(unittest.TestCase):
    """Запись accounts.json не должна терять данные молча."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._path = os.path.join(self._dir, "accounts.json")
        self._patch = mock.patch.object(botmod, "ACCOUNTS_FILE", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for name in os.listdir(self._dir):
            try:
                os.unlink(os.path.join(self._dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass

    def test_save_leaves_backup_of_previous_file(self):
        m = botmod.BotManager()
        m.add_bot("TOLYATTI", "Pervyj_Save", "55337q", default_task="stop")
        m.save_accounts(force=True)
        self.assertTrue(os.path.exists(self._path))

        # add_bot сам сохраняет состояние, поэтому копия пишется здесь.
        m.add_bot("TOLYATTI", "Vtoroj_Save", "55337q", default_task="stop")

        bak = self._path + ".bak"
        self.assertTrue(os.path.exists(bak), "резервная копия не создана")
        with open(bak, encoding="utf-8") as f:
            old = json.load(f)
        self.assertEqual([x["nickname"] for x in old], ["Pervyj_Save"])

    def test_no_temp_file_left_behind(self):
        m = botmod.BotManager()
        m.add_bot("TOLYATTI", "Tmp_Save", "55337q", default_task="stop")
        m.save_accounts(force=True)
        self.assertFalse(os.path.exists(self._path + ".tmp"))
