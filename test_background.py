"""Запуск сервисом без терминала.

Раньше на пустом реестре main() уходил в интерактивное меню и упирался в
input() без TTY: EOFError, код возврата 1, systemd перезапускает — и так
по кругу. Первый деплой на чистую машину не поднимался вообще.
"""

import ast
import os
import unittest

SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")


def _main_source():
    with open(SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("main() не найдена")


class BackgroundModeTests(unittest.TestCase):
    def test_background_defined_before_branches(self):
        """Признак должен считаться до ветвления, иначе NameError."""
        main = _main_source()
        assign_line = None
        for node in ast.walk(main):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "background":
                        assign_line = node.lineno
        self.assertIsNotNone(assign_line, "background не вычисляется")

        # Все обращения к background должны быть ниже присваивания.
        for node in ast.walk(main):
            if isinstance(node, ast.Name) and node.id == "background":
                if isinstance(node.ctx, ast.Load):
                    self.assertGreater(
                        node.lineno, assign_line,
                        "background используется до присваивания",
                    )

    def test_single_assignment(self):
        """Присваивание должно быть одно — разъехавшиеся копии уже ломали."""
        main = _main_source()
        count = 0
        for node in ast.walk(main):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "background":
                        count += 1
        self.assertEqual(count, 1)

    def test_first_run_guarded_by_background(self):
        """На пустом реестре меню не должно открываться в фоне."""
        with open(SOURCE, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("elif background:", src)
        # interactive_menu вызывается только в ветке else после неё.
        idx = src.index("elif background:")
        tail = src[idx:idx + 400]
        self.assertIn("interactive_menu(manager)", tail)

    def test_interactive_menu_not_called_in_background(self):
        """В фоне input() не должен вызываться ни разу."""
        with open(SOURCE, encoding="utf-8") as fh:
            src = fh.read()
        # Финальный цикл ожидания идёт сразу после проверки background.
        # Ищем именно отдельный `if`, а не подстроку внутри `elif`.
        idx = src.index("\n    if background:")
        self.assertIn("time.sleep(1)", src[idx:idx + 300])


if __name__ == "__main__":
    unittest.main(verbosity=2)
