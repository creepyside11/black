"""Сканер меню: перебор пунктов и переход во второе меню.

Живой сервер дорог — каждый перезаход это риск кика 25, поэтому перебор
проверяется на фейковом мире, который ведёт себя как настоящий: пикап
всегда отдаёт ПЕРВОЕ меню, а вложенное получается только ответом на пункт.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_diag"))

import menuscan  # noqa: E402


MENU1 = {
    "id": 0, "style": 5, "title": "Аренда транспорта",
    "info": "номер\tнаименование\n#1\tСкутер\n#2\tМопед\n#3\tКвадроцикл",
    "button1": "Выбрать", "button2": "Закрыть",
}
MENU2 = {
    "id": 0, "style": 5, "title": "Срок аренды",
    "info": "номер\tсрок\n#1\t1 час\n#2\t24 часа",
    "button1": "Выбрать", "button2": "Закрыть",
}


class FakeWorld:
    """Пикап отдаёт первое меню; второе — только ответом на пункт."""

    def __init__(self, follow_for=None, nested_follow=None):
        self.follow_for = follow_for or {}
        self.nested_follow = nested_follow or {}
        self.pending = None
        self.pokes = 0
        self.answers = []

    def poke(self, pid):
        self.pokes += 1
        self.pending = MENU1

    def wait_dialog(self, limit=10.0):
        dialog, self.pending = self.pending, None
        return dialog

    def close_dialog(self, dialog):
        pass

    def answer(self, dialog, item):
        self.answers.append((dialog["title"], item))
        if dialog["title"] == MENU1["title"]:
            self.pending = self.follow_for.get(item)
        elif dialog["title"] == MENU2["title"]:
            self.pending = self.nested_follow.get(item)

    def away(self):
        pass


class ScanTests(unittest.TestCase):
    def _run(self, world, max_items=6):
        logs = []
        runner = menuscan.build(
            world.poke, world.wait_dialog, world.close_dialog, world.answer,
            logs.append, teleport_away=world.away, max_items=max_items,
            wait=0.0, pause=lambda _s: None,
        )
        report = runner(1)
        return report, logs

    def test_finds_working_item(self):
        world = FakeWorld(follow_for={1: MENU2})
        report, _ = self._run(world)
        working = [e for e in report if e["outcome"] == "follow_up"]
        self.assertEqual(len(working), 1)
        self.assertEqual(working[0]["item"], 1)

    def test_dead_items_are_silence(self):
        """Меню из 4 строк, рабочий пункт один — остальные три молчат."""
        world = FakeWorld(follow_for={2: MENU2})
        report, _ = self._run(world)
        first_level = [e for e in report if e["depth"] == 1]
        silent = [e for e in first_level if e["outcome"] == "silence"]
        self.assertEqual(len(silent), 3)

    def test_second_menu_is_scanned_too(self):
        world = FakeWorld(follow_for={0: MENU2})
        report, _ = self._run(world)
        depth2 = [e for e in report if e["depth"] == 2]
        self.assertTrue(depth2, "второе меню должно разбираться")

    def test_second_menu_reopen_does_not_fall_back_to_first(self):
        """Вложенное меню переоткрывается ответом на пункт, а не пикапом.

        Раньше общий open_menu(pid) возвращал первое меню, и разбор второго
        уровня молча зацикливался на нём.
        """
        world = FakeWorld(follow_for={1: MENU2})
        report, _ = self._run(world)
        depth2 = [e for e in report if e["depth"] == 2]
        labels = {e["label"] for e in depth2}
        self.assertTrue(all(lb.endswith("Срок аренды") for lb in labels), labels)

    def test_depth_limited_to_two(self):
        world = FakeWorld(follow_for={0: MENU2}, nested_follow={0: MENU1})
        report, _ = self._run(world)
        self.assertTrue(all(e["depth"] <= 2 for e in report))

    def test_max_items_respected(self):
        world = FakeWorld()
        report, _ = self._run(world, max_items=2)
        first_level = [e for e in report if e["depth"] == 1]
        self.assertLessEqual(len(first_level), 2)

    def test_menu_not_opening_is_reported(self):
        world = FakeWorld()
        world.poke = lambda pid: None
        logs = []
        runner = menuscan.build(
            world.poke, world.wait_dialog, world.close_dialog, world.answer,
            logs.append, wait=0.0, pause=lambda _s: None,
        )
        report = runner(1)
        self.assertEqual(report, [])
        self.assertTrue(any("не открылось" in line for line in logs))

    def test_rows_of_skips_blank(self):
        self.assertEqual(menuscan.rows_of("a\n\n b \n"), ["a", " b "])


if __name__ == "__main__":
    unittest.main(verbosity=2)
