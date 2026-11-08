"""Меню: разбор строк, выбор пункта, вложенные диалоги, /mm.

Реальные данные взяты из farm.log — это настоящее меню Black Russia,
которое приходит на команду /mm.
"""

import unittest

from logic.menu import (
    Decision,
    dialog_signature,
    MenuAnswerer,
    STYLE_LIST,
    STYLE_TABLIST,
    STYLE_TABLIST_HEADERS,
    decide_stats_flow,
    find_row,
    find_row_index,
    looks_like_player_menu,
    looks_like_stats_data,
    parse_rows,
    parse_stats,
    strip_colors,
    strip_numbering,
)

# Настоящее меню из farm.log, строки 74-88.
REAL_TITLE = "{E0584B}BLACK RUSSIA {ffffff}| Меню игрока"
REAL_INFO = """{E0584B}1. {ffffff}Статистика
{E0584B}2. {ffffff}Список команд
{E0584B}3. {ffffff}Личные настройки
{E0584B}4. {ffffff}Настройки безопасности
{E0584B}5. {ffffff}Жалоба/Вопрос
{E0584B}6. {ffffff}Улучшения
{E0584B}7. {ffffff}Правила сервера
{E0584B}8. {ffffff}Изменить имя
{E0584B}9. {ffffff}Промокод
{E0584B}10. {33cc66}Донат"""


class FakeSession:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send_dialog_response(self, dialog_id, response=1, list_item=0, text=""):
        if not self.connected:
            return False
        self.sent.append({
            "dialog_id": dialog_id,
            "response": response,
            "list_item": list_item,
            "text": text,
        })
        return True


class StripTests(unittest.TestCase):
    def test_colors_removed(self):
        self.assertEqual(strip_colors("{E0584B}Статистика"), "Статистика")

    def test_numbering_removed(self):
        self.assertEqual(strip_numbering("{E0584B}1. {ffffff}Статистика"), "Статистика")
        self.assertEqual(strip_numbering("10. Донат"), "Донат")
        self.assertEqual(strip_numbering("3) Настройки"), "Настройки")

    def test_text_without_numbering_untouched(self):
        self.assertEqual(strip_numbering("Просто строка"), "Просто строка")

    def test_number_in_middle_kept(self):
        """«2/8 exp» не должно терять цифры — это не нумерация."""
        self.assertEqual(strip_numbering("Опыт 2/8"), "Опыт 2/8")


class RowParsingTests(unittest.TestCase):
    def test_real_menu_rows(self):
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0].text, "Статистика")
        self.assertEqual(rows[0].index, 0)
        self.assertEqual(rows[0].number, 1, "номер в тексте сохраняется отдельно")
        self.assertEqual(rows[9].text, "Донат")
        self.assertEqual(rows[9].index, 9)

    def test_index_differs_from_visible_number(self):
        """Главная ловушка: «1. Статистика» — это list_item 0, а не 1."""
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        stat = find_row(rows, "статистик")
        self.assertEqual(stat.number, 1)
        self.assertEqual(stat.index, 0)
        self.assertNotEqual(stat.index, stat.number)

    def test_tablist_headers_drops_header_row(self):
        info = "Имя\tУровень\nСтатистика\t5\nКоманды\t6"
        rows = parse_rows(info, STYLE_TABLIST_HEADERS)
        self.assertEqual(len(rows), 2, "шапка не должна считаться строкой")
        # Колонки остаются в тексте — по ним ищется пункт.
        self.assertTrue(rows[0].text.startswith("Статистика"))
        self.assertEqual(rows[0].index, 0)
        self.assertIsNotNone(find_row(rows, "статистик"))

    def test_tablist_without_headers_keeps_all(self):
        info = "Статистика\t5\nКоманды\t6"
        rows = parse_rows(info, STYLE_TABLIST)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].index, 0)

    def test_empty_info(self):
        self.assertEqual(parse_rows("", STYLE_LIST), [])
        self.assertEqual(parse_rows(None, STYLE_LIST), [])

    def test_blank_lines_skipped(self):
        rows = parse_rows("Статистика\n\n\nКоманды", STYLE_LIST)
        self.assertEqual(len(rows), 2)

    def test_find_row_case_insensitive(self):
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        self.assertIsNotNone(find_row(rows, "СТАТИСТИК"))
        self.assertIsNotNone(find_row(rows, "донат"))

    def test_find_row_missing(self):
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        self.assertIsNone(find_row(rows, "нет-такого-пункта"))
        self.assertIsNone(find_row_index(rows, "нет-такого-пункта"))

    def test_find_row_exact(self):
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        self.assertIsNone(find_row(rows, "статист", exact=True))
        self.assertIsNotNone(find_row(rows, "статистика", exact=True))


class RecogniseTests(unittest.TestCase):
    def test_real_player_menu_recognised(self):
        rows = parse_rows(REAL_INFO, STYLE_LIST)
        self.assertTrue(looks_like_player_menu(REAL_TITLE, rows))

    def test_old_checks_would_have_failed(self):
        """Регрессия: раньше искали «главное меню» и «меню персонажа»."""
        title_low = REAL_TITLE.lower()
        self.assertNotIn("главное меню", title_low)
        self.assertNotIn("меню персонажа", title_low)
        self.assertIn("меню игрока", title_low)

    def test_menu_without_stats_row_is_not_player_menu(self):
        rows = parse_rows("Что-то\nДругое", STYLE_LIST)
        self.assertFalse(looks_like_player_menu(REAL_TITLE, rows))

    def test_stats_screen_recognised(self):
        self.assertTrue(looks_like_stats_data("Статистика", "Уровень: 3"))
        self.assertTrue(looks_like_stats_data("", "Опыт: 2/8"))
        self.assertTrue(looks_like_stats_data("", "Баланс: 1500"))

    def test_menu_is_not_stats_screen(self):
        """В меню есть слово «Статистика», но это не экран данных."""
        self.assertFalse(looks_like_stats_data(REAL_TITLE, REAL_INFO))


class DecideTests(unittest.TestCase):
    def test_menu_picks_stats_row(self):
        decision = decide_stats_flow(2, REAL_TITLE, REAL_INFO, stats_pending=True)
        self.assertEqual(decision.action, "pick")
        self.assertEqual(decision.list_item, 0)
        self.assertEqual(decision.response, 1, "левая кнопка «Выбрать»")
        self.assertEqual(decision.screen, "menu")

    def test_stats_screen_closes(self):
        decision = decide_stats_flow(
            0, "Статистика", "Уровень: 3\nОпыт: 2/8", stats_pending=True)
        self.assertEqual(decision.action, "close")
        self.assertEqual(decision.response, 0, "правая кнопка «Закрыть»")
        self.assertEqual(decision.screen, "stats")

    def test_menu_wins_over_stats_screen(self):
        """Меню проверяется первым — иначе данные не покажутся."""
        decision = decide_stats_flow(2, REAL_TITLE, REAL_INFO, stats_pending=True)
        self.assertEqual(decision.screen, "menu")
        self.assertEqual(decision.action, "pick")

    def test_player_menu_answered_even_when_not_requested(self):
        """Меню игрока отвечаем всегда, даже если /mm ушёл вручную.

        Раньше здесь стоял ранний выход по stats_pending, и /mm, отправленный
        не таймером, оставался без ответа: диалог висел поверх игры, а сервер
        на следующую команду отвечал «Закройте диалог!».
        """
        decision = decide_stats_flow(
            2, REAL_TITLE, REAL_INFO, stats_pending=False)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.screen, "menu")
        self.assertEqual(decision.action, "pick")
        self.assertEqual(decision.list_item, 0, "пункт «Статистика»")

    def test_unrelated_dialog_ignored(self):
        self.assertIsNone(
            decide_stats_flow(0, "Anti-Cheat", "Код: 25", stats_pending=True))

    def test_menu_without_stats_row_closes(self):
        info = "1. Команды\n2. Настройки"
        decision = decide_stats_flow(2, REAL_TITLE, info, stats_pending=True)
        self.assertEqual(decision.action, "close")
        self.assertEqual(decision.screen, "menu")


class ParseStatsTests(unittest.TestCase):
    def test_level_and_exp(self):
        stats = parse_stats("Уровень: 3\nОпыт: 2/8")
        self.assertEqual(stats["level"], 3)
        self.assertEqual(stats["exp"], "2/8")

    def test_russian_exp_words(self):
        stats = parse_stats("Игровой уровень: 5\nОчки опыта: 12 из 40")
        self.assertEqual(stats["level"], 5)
        self.assertEqual(stats["exp"], "12/40")

    def test_money_with_spaces(self):
        stats = parse_stats("Наличные деньги: 15 000 руб.")
        self.assertEqual(stats["money"], 15000)

    def test_bank_account(self):
        stats = parse_stats("Банковский счёт: 20000 руб.")
        self.assertEqual(stats["money"], 20000)

    def test_no_data(self):
        stats = parse_stats("ничего полезного")
        self.assertIsNone(stats["level"])
        self.assertIsNone(stats["exp"])
        self.assertIsNone(stats["money"])

    def test_colors_do_not_break_parsing(self):
        stats = parse_stats("{FFFFFF}Уровень: {FFFF00}4{FFFFFF}\nОпыт: 1/10")
        self.assertEqual(stats["level"], 4)
        self.assertEqual(stats["exp"], "1/10")


class AnswererTests(unittest.TestCase):
    def test_answers_once(self):
        session = FakeSession()
        answerer = MenuAnswerer()
        self.assertTrue(answerer.answer(session, 0, list_item=0, response=1))
        self.assertFalse(answerer.answer(session, 0, list_item=0, response=1))
        self.assertEqual(len(session.sent), 1)

    def test_different_screen_is_not_a_duplicate(self):
        """Все диалоги BR приходят с id 0 — экраны различаем по содержимому."""
        session = FakeSession()
        answerer = MenuAnswerer()
        first = dialog_signature(2, REAL_TITLE, REAL_INFO)
        second = dialog_signature(0, "Статистика", "Уровень: 3")
        self.assertTrue(answerer.answer(session, 0, list_item=0, signature=first))
        self.assertTrue(answerer.answer(session, 0, list_item=0, signature=second))
        self.assertEqual(len(session.sent), 2)

    def test_same_screen_twice_is_duplicate(self):
        """Повторная присылка того же экрана — второй ответ не шлём."""
        session = FakeSession()
        answerer = MenuAnswerer()
        sig = dialog_signature(2, REAL_TITLE, REAL_INFO)
        self.assertTrue(answerer.answer(session, 0, list_item=0, signature=sig))
        self.assertFalse(answerer.answer(session, 0, list_item=0, signature=sig))
        self.assertEqual(len(session.sent), 1)

    def test_duplicate_expires(self):
        """Сервер прислал меню повторно — значит ответ не дошёл, шлём снова."""
        session = FakeSession()
        answerer = MenuAnswerer(ttl=0.05)
        answerer.answer(session, 0, list_item=0, response=1)
        import time
        time.sleep(0.1)
        self.assertTrue(answerer.answer(session, 0, list_item=0, response=1))

    def test_force_bypasses_dedupe(self):
        session = FakeSession()
        answerer = MenuAnswerer()
        answerer.answer(session, 0, list_item=0)
        self.assertTrue(answerer.answer(session, 0, list_item=0, force=True))

    def test_disconnected_session_sends_nothing(self):
        session = FakeSession(connected=False)
        answerer = MenuAnswerer()
        self.assertFalse(answerer.answer(session, 0, list_item=0))
        self.assertEqual(session.sent, [])

    def test_none_session_is_safe(self):
        self.assertFalse(MenuAnswerer().answer(None, 0, list_item=0))


class NestedFlowTests(unittest.TestCase):
    """Полный проход: /mm -> меню -> «Статистика» -> данные -> закрыть."""

    def _run(self):
        session = FakeSession()
        answerer = MenuAnswerer()
        log = []

        # Экран 1: меню пришло после /mm
        first = decide_stats_flow(2, REAL_TITLE, REAL_INFO, stats_pending=True)
        self.assertEqual(first.action, "pick")
        answerer.answer(session, 0, list_item=first.list_item,
                        response=first.response, note=first.note,
                        signature=dialog_signature(2, REAL_TITLE, REAL_INFO))

        # Экран 2: вложенный диалог с данными. id тот же — 0, как в BR.
        stats_info = "Уровень: 3\nОпыт: 2/8\nБаланс: 1500"
        second = decide_stats_flow(0, "Статистика", stats_info, stats_pending=True)
        self.assertEqual(second.action, "close")
        parsed = parse_stats(stats_info)
        answerer.answer(session, 0, list_item=second.list_item,
                        response=second.response, note=second.note,
                        signature=dialog_signature(0, "Статистика", stats_info))
        return session, parsed

    def test_two_answers_in_order(self):
        session, _ = self._run()
        self.assertEqual(len(session.sent), 2, "должно быть ровно два ответа")
        # Первый — выбор пункта левой кнопкой
        self.assertEqual(session.sent[0]["response"], 1)
        self.assertEqual(session.sent[0]["list_item"], 0)
        # Второй — закрытие правой кнопкой
        self.assertEqual(session.sent[1]["response"], 0)

    def test_stats_parsed(self):
        _, parsed = self._run()
        self.assertEqual(parsed["level"], 3)
        self.assertEqual(parsed["exp"], "2/8")
        self.assertEqual(parsed["money"], 1500)

    def test_dialog_id_comes_from_server(self):
        """Отвечать надо тем id, что прислал сервер, а не жёстким 0/1."""
        session = FakeSession()
        answerer = MenuAnswerer()
        decision = decide_stats_flow(2, REAL_TITLE, REAL_INFO, stats_pending=True)
        answerer.answer(session, 7, list_item=decision.list_item,
                        response=decision.response,
                        signature=dialog_signature(2, REAL_TITLE, REAL_INFO))
        self.assertEqual(session.sent[0]["dialog_id"], 7)

    def test_second_screen_does_not_reopen_menu(self):
        """На экране данных уже нет выбора пункта — только закрытие."""
        decision = decide_stats_flow(
            0, "Статистика", "Уровень: 3", stats_pending=True)
        self.assertEqual(decision.action, "close")
        self.assertNotEqual(decision.action, "pick")


class StatsNumbersTests(unittest.TestCase):
    """Опыт до следующего уровня считается из той же строки."""

    def test_exp_split_into_numbers(self):
        stats = parse_stats("Уровень: 3\nОпыт: 12/40")
        self.assertEqual(stats["exp_cur"], 12)
        self.assertEqual(stats["exp_next"], 40)
        self.assertEqual(stats["exp_left"], 28)

    def test_exp_left_never_negative(self):
        stats = parse_stats("Уровень: 3\nОпыт: 50/40")
        self.assertEqual(stats["exp_left"], 0)

    def test_no_exp_keeps_fields_empty(self):
        stats = parse_stats("Уровень: 3")
        self.assertIsNone(stats["exp_cur"])
        self.assertIsNone(stats["exp_left"])

    def test_exp_words(self):
        stats = parse_stats("Игровой уровень: 5\nОчки опыта: 12 из 40")
        self.assertEqual(stats["exp_cur"], 12)
        self.assertEqual(stats["exp_next"], 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
