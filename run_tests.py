#!/usr/bin/env python3
"""Запуск всех юнит-тестов репозитория.

`python -m unittest discover` не годится: он лезет в вендоренный urllib3 и
падает на его emscripten-модуле, а до этого — в живые диагностические
скрипты, которые реально подключаются к игровым серверам и пишут в
accounts.json. Поэтому берём только модули из корня, в которых есть
unittest, и запускаем их явно.

Живые скрипты лежат в _diag/live/ и запускаются руками.
"""

import glob
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))


def find_tests() -> list[str]:
    modules = []
    for path in sorted(glob.glob(os.path.join(ROOT, "test_*.py"))):
        name = os.path.basename(path)[:-3]
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                source = fh.read()
        except OSError:
            continue
        if "unittest" not in source:
            continue
        modules.append(name)
    return modules


def main() -> int:
    sys.path.insert(0, ROOT)
    # BotManager под тестами пишет в accounts.test.json, а не в боевой
    # accounts.json. Один прогон без этого уже стирал реестр.
    os.environ.setdefault("BR_TEST_MODE", "1")
    modules = find_tests()
    if not modules:
        print("Не нашёл тестов")
        return 1
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in modules:
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as exc:
            print(f"[!] Не загрузил {name}: {exc}")
    print(f"Модулей: {len(modules)}")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
