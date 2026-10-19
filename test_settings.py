"""Настройки бота: базовый пароль и задача по умолчанию.

Базовый пароль нужен, чтобы при автозамене забаненного аккаунта и при
массовой регистрации не приходилось вводить пароль руками каждый раз.
"""

import json
import os
import tempfile
import unittest

from logic.settings import DEFAULT_PASSWORD, DEFAULT_TASK, KNOWN_TASKS, Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        self.path = self._tmp.name
        self.s = Settings(self.path)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_defaults(self):
        self.assertEqual(self.s.base_password, DEFAULT_PASSWORD)
        self.assertEqual(self.s.default_task, DEFAULT_TASK)
        self.assertEqual(self.s.batch_limit, 50)

    def test_password_survives_reload(self):
        self.s.base_password = "superpass1"
        again = Settings(self.path)
        self.assertEqual(again.base_password, "superpass1")

    def test_password_is_stripped(self):
        self.s.base_password = "  pass with spaces  "
        self.assertEqual(self.s.base_password, "pass with spaces")

    def test_task_survives_reload(self):
        self.s.default_task = "farm"
        self.assertEqual(Settings(self.path).default_task, "farm")

    def test_unknown_task_falls_back(self):
        self.s.default_task = "неведомая"
        self.assertEqual(self.s.default_task, DEFAULT_TASK)

    def test_all_known_tasks_accepted(self):
        for task in KNOWN_TASKS:
            self.s.default_task = task
            self.assertEqual(self.s.default_task, task)

    def test_batch_limit_clamped(self):
        self.s.batch_limit = 0
        self.assertEqual(self.s.batch_limit, 1)
        self.s.batch_limit = 99999
        self.assertEqual(self.s.batch_limit, 500)
        self.s.batch_limit = "abc"
        self.assertEqual(self.s.batch_limit, 500)

    def test_password_or_prefers_base(self):
        self.s.base_password = "base123"
        self.assertEqual(self.s.password_or("другой"), "base123")

    def test_password_or_uses_fallback_when_base_empty(self):
        self.s.base_password = ""
        self.assertEqual(self.s.password_or("другой"), "другой")

    def test_password_or_never_empty(self):
        """Пустой пароль — это отказ add_bot создавать аккаунт."""
        self.s.base_password = ""
        self.assertTrue(self.s.password_or(""))

    def test_broken_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{не json")
        s = Settings(self.path)
        self.assertEqual(s.base_password, DEFAULT_PASSWORD)

    def test_non_dict_file_does_not_crash(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(["список"], f)
        s = Settings(self.path)
        self.assertEqual(s.base_password, DEFAULT_PASSWORD)

    def test_save_leaves_valid_json(self):
        self.s.base_password = "x1"
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["base_password"], "x1")
        self.assertFalse(os.path.exists(self.path + ".tmp"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
