"""Разбор сообщений о бане: кто забанил, за что, на сколько."""

import unittest

from logic.baninfo import parse_ban


class ManualBanTests(unittest.TestCase):
    """Ручной бан приходит в чат со всеми подробностями."""

    def test_real_line_from_log(self):
        info = parse_ban(
            "Администратор Yaroslav_Kapeka навсегда заблокировал игрока "
            "Kyndal_Golodnjak. Причина: BOT"
        )
        self.assertEqual(info.kind, "manual")
        self.assertEqual(info.admin, "Yaroslav_Kapeka")
        self.assertEqual(info.reason, "BOT")
        self.assertEqual(info.duration, "навсегда")
        self.assertEqual(info.target, "Kyndal_Golodnjak")
        self.assertTrue(info.is_bot_ban)

    def test_issuer_signature_is_split_off(self):
        info = parse_ban(
            "Администратор Maks_Almazov навсегда заблокировал игрока "
            "Juan_Fionin. Причина: BOT | by S. Antipov"
        )
        self.assertEqual(info.admin, "Maks_Almazov")
        self.assertEqual(info.issuer, "S. Antipov")
        self.assertEqual(info.reason, "BOT", "подпись попала в причину")

    def test_temporary_ban_duration(self):
        info = parse_ban(
            "Модератор Test_Mod заблокировал игрока Nick_One на 7 дней. "
            "Причина: читы"
        )
        self.assertEqual(info.kind, "manual")
        self.assertEqual(info.admin, "Test_Mod")
        self.assertEqual(info.duration, "на 7 дн.")
        self.assertEqual(info.reason_human, "Читы")

    def test_hours_and_minutes(self):
        hour = parse_ban("Админ A_B заблокировал игрока C_D на 2 часа. Причина: флуд")
        self.assertEqual(hour.duration, "на 2 ч.")
        minute = parse_ban("Админ A_B заблокировал игрока C_D на 30 минут. Причина: флуд")
        self.assertEqual(minute.duration, "на 30 мин.")

    def test_dative_case_player(self):
        """«выдал бан игроку X» — другой падеж, раньше не разбирался."""
        info = parse_ban("Администратор X_Y выдал бан игроку Z_Z. Причина: мультиаккаунт")
        self.assertEqual(info.kind, "manual")
        self.assertEqual(info.admin, "X_Y")
        self.assertEqual(info.reason_human, "Мультиаккаунт")

    def test_short_role_words(self):
        for role in ("Админ", "Модератор", "Хелпер"):
            info = parse_ban(f"{role} Some_Name забанил игрока Bad_Guy. Причина: BOT")
            self.assertEqual(info.kind, "manual", role)
            self.assertEqual(info.admin, "Some_Name", role)

    def test_colors_stripped(self):
        info = parse_ban(
            "{E0584B}Администратор {FFFFFF}Cool_Admin{E0584B} навсегда "
            "заблокировал игрока {FFFFFF}Some_Player{E0584B}. Причина: {FFFFFF}BOT"
        )
        self.assertEqual(info.admin, "Cool_Admin")
        self.assertIn("BOT", info.reason)


class AutoBanTests(unittest.TestCase):
    """Автоматический бан — безлично, без админа и причины."""

    def test_plain_auto_ban(self):
        info = parse_ban("Ваш игровой аккаунт заблокирован, вход невозможен.")
        self.assertEqual(info.kind, "auto")
        self.assertEqual(info.admin, "")
        self.assertIn("без админа", info.summary)

    def test_auto_ban_in_dialog(self):
        info = parse_ban(
            "{E0584B}BLACK RUSSIA {FFFFFF}| Аккаунт заблокирован | "
            "Ваш игровой аккаунт был заблокирован администрацией за нарушение"
        )
        self.assertEqual(info.kind, "auto")
        self.assertEqual(info.admin, "")

    def test_connection_banned_packet(self):
        info = parse_ban("Пакет CONNECTION_BANNED (0x19)")
        self.assertEqual(info.kind, "auto")


class EdgeCaseTests(unittest.TestCase):
    def test_empty_input(self):
        info = parse_ban("")
        self.assertEqual(info.kind, "unknown")
        self.assertEqual(info.summary, "причина не распознана")

    def test_none_input(self):
        info = parse_ban(None)
        self.assertEqual(info.kind, "unknown")

    def test_ordinary_chat_is_not_a_ban(self):
        info = parse_ban("Привет всем, кто на ферме?")
        self.assertEqual(info.kind, "unknown")
        self.assertFalse(info.is_bot_ban)

    def test_word_bot_alone_is_not_a_ban(self):
        """«Информация о работах» ловилось на подстроку «бот» — так нельзя."""
        info = parse_ban("Информация о работах доступна в меню")
        self.assertEqual(info.kind, "unknown")

    def test_as_dict_roundtrip(self):
        info = parse_ban(
            "Администратор A_B навсегда заблокировал игрока C_D. Причина: BOT")
        data = info.as_dict()
        self.assertEqual(data["admin"], "A_B")
        self.assertEqual(data["kind"], "manual")
        self.assertIsInstance(data["tags"], list)

    def test_summary_mentions_admin(self):
        info = parse_ban(
            "Администратор A_B навсегда заблокировал игрока C_D. Причина: BOT")
        self.assertIn("A_B", info.summary)
        self.assertIn("навсегда", info.summary)

    def test_is_bot_ban_flags_cheats(self):
        info = parse_ban("Админ A_B забанил игрока C_D. Причина: читы")
        self.assertTrue(info.is_bot_ban)

    def test_is_bot_ban_false_for_flood(self):
        info = parse_ban("Админ A_B забанил игрока C_D. Причина: флуд")
        self.assertFalse(info.is_bot_ban)


if __name__ == "__main__":
    unittest.main(verbosity=2)
