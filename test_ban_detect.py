"""Детектор ограничения входа: не должен ловить обычные рассылки.

Живой случай: сообщение про зарплату («находиться в игре минимум 20 минут»)
обрывало сессию и ставило паузу 25 минут посреди телепорта.
"""

import unittest

import bot as botmod


class FreqBanTests(unittest.TestCase):
    def test_salary_message_is_not_ban(self):
        text = ("Для получения зарплаты необходимо находиться в игре минимум "
                "20 минут")
        self.assertFalse(botmod.is_freq_ban_text(text))

    def test_event_announcement_is_not_ban(self):
        for text in (
            "Через 10 минут начнётся игра Танковые баталии в компьютерном клубе",
            "Через 5 минут начнётся мероприятие",
            "Телепортация на место проведения мероприятия была автоматически закрыта",
        ):
            self.assertFalse(botmod.is_freq_ban_text(text), text)

    def test_real_restrictions_are_detected(self):
        for text in (
            "Вы были заблокированы за частые подключения",
            "Подождите 20 минут перед следующим входом",
            "Повторите попытку через 15 минут",
            "Слишком много подключений с вашего IP",
            "Обнаружены частые входы с вашего аккаунта",
            "Попробуйте зайти позже, идёт проверка",
        ):
            self.assertTrue(botmod.is_freq_ban_text(text), text)

    def test_color_codes_stripped(self):
        self.assertTrue(
            botmod.is_freq_ban_text("{FF0000}Частые подключения{FFFFFF} к серверу")
        )

    def test_empty(self):
        self.assertFalse(botmod.is_freq_ban_text(""))
        self.assertFalse(botmod.is_freq_ban_text(None))

    def test_marker_inside_salary_message_still_not_ban(self):
        """Даже если в рассылке про зарплату есть «подождите» — не бан."""
        text = "Зарплата: подождите 20 минут, пока начислится"
        self.assertFalse(botmod.is_freq_ban_text(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AntiCheatKickTests(unittest.TestCase):
    """Кик античита должен помечать аккаунт, а не гонять перезаходы.

    Живой случай: сервер пишет «Вы были кикнуты по подозрению использования
    постороннего ПО», а в триггерах стояло «постороннее по» — падеж не
    совпадал, строка не ловилась. Аккаунт оставался «живым», и бот заходил
    по кругу: кик, 18 секунд, снова кик.
    """

    BODY = (
        "{FFFFFF}Вы были кикнуты по подозрению использования постороннего ПО\n"
        "Если Вы были кикнуты - без причины, пожалуйста обратитесь на форум в "
        "технический раздел.\n\nКод: {FFFF00}25{FFFFFF}\n"
        "PacketLoss: {FFFF00}0.535947{FFFFFF}\nPing: {FFFF00}173"
    )

    def test_live_message_is_detected(self):
        self.assertEqual(
            botmod.anticheat_kick("{FF0000}Anti-Cheat", self.BODY), 25
        )

    def test_code_is_extracted(self):
        body = "Вы были кикнуты по подозрению использования постороннего ПО\nКод: 2"
        self.assertEqual(botmod.anticheat_kick("Anti-Cheat", body), 2)

    def test_code_absent_is_zero(self):
        body = "Вы были кикнуты по подозрению использования постороннего ПО"
        self.assertEqual(botmod.anticheat_kick("Anti-Cheat", body), 0)

    def test_other_titles_ignored(self):
        self.assertIsNone(botmod.anticheat_kick("Основное меню", self.BODY))

    def test_normal_dialog_ignored(self):
        self.assertIsNone(
            botmod.anticheat_kick(
                "{E0584B}BLACK RUSSIA {FFFFFF}| Основное меню",
                "#1 Информация о работах",
            )
        )

    def test_case_insensitive(self):
        self.assertEqual(
            botmod.anticheat_kick("ANTI-CHEAT", "вы были КИКНУТЫ за постороннее ПО"),
            0,
        )

    def test_title_variants(self):
        for title in ("Anti-Cheat", "ANTI CHEAT", "anticheat", "{FF0000}Anti-Cheat"):
            self.assertIsNotNone(
                botmod.anticheat_kick(title, "постороннего ПО"), title
            )
