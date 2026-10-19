"""Несколько админов Telegram.

Раньше принимался один id: второй админ получал «Доступ запрещен», а
повторный флаг --admin затирал предыдущий.
"""

import unittest

from tg_controller import TelegramBotController as T


class ParseTests(unittest.TestCase):
    def test_single_int(self):
        self.assertEqual(T._parse_admins(8282599467), [8282599467])

    def test_single_string(self):
        self.assertEqual(T._parse_admins("8282599467"), [8282599467])

    def test_list(self):
        self.assertEqual(
            T._parse_admins([8282599467, 7973988177]),
            [8282599467, 7973988177],
        )

    def test_comma_string(self):
        self.assertEqual(
            T._parse_admins("8282599467,7973988177"),
            [8282599467, 7973988177],
        )

    def test_spaces_and_semicolons(self):
        self.assertEqual(
            T._parse_admins("8282599467; 7973988177 123456789"),
            [8282599467, 7973988177, 123456789],
        )

    def test_duplicates_removed(self):
        self.assertEqual(T._parse_admins([1, 1, 2]), [1, 2])

    def test_garbage_dropped(self):
        self.assertEqual(T._parse_admins("abc,123,,-5,0"), [123])

    def test_none_and_empty(self):
        self.assertEqual(T._parse_admins(None), [])
        self.assertEqual(T._parse_admins(""), [])


class AuthorizeTests(unittest.TestCase):
    def _controller(self, admins):
        """Контроллер без сети: подменяем то, что лезет в Telegram."""
        obj = T.__new__(T)
        obj.admin_ids = T._parse_admins(admins)
        obj.admin_id = obj.admin_ids[0] if obj.admin_ids else None
        return obj

    def test_both_admins_allowed(self):
        c = self._controller([8282599467, 7973988177])
        self.assertTrue(c._is_authorized(8282599467))
        self.assertTrue(c._is_authorized(7973988177))

    def test_stranger_denied(self):
        c = self._controller([8282599467, 7973988177])
        self.assertFalse(c._is_authorized(12345))

    def test_no_admins_means_open(self):
        self.assertTrue(self._controller(None)._is_authorized(999))

    def test_legacy_admin_id_attribute(self):
        """Внешний код читает admin_id — он должен остаться осмысленным."""
        c = self._controller([111, 222])
        self.assertEqual(c.admin_id, 111)


if __name__ == "__main__":
    unittest.main(verbosity=2)
