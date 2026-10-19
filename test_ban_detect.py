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
