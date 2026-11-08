#!/usr/bin/env python3
# Black Russia Single-Account Interactive Runner
# Usage:
#   python3 -m br                  -> Interactive Console UI
#   python3 -m br --tg <TOKEN>     -> Telegram Interactive Controller
#   python3 br/main.py             -> то же самое напрямую

import os
import sys

# `int | None` и встроенные генерики в проекте — Python 3.10+.
# На старом интерпретаторе лучше сказать это словами, чем отдать
# TypeError из середины импорта.
if sys.version_info < (3, 10):
    sys.exit(
        "Нужен Python 3.10 или новее, сейчас %d.%d. "
        "Termux: pkg install python; Debian/Ubuntu: apt install python3; "
        "Windows: python.org/downloads" % sys.version_info[:2]
    )

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(ROOT_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _parse_args(argv):
    """Разбор флагов без арифметики по индексам.

    Старый вариант считал позицию значения как index+2 по sys.argv[1:],
    поэтому `--tg TOKEN --admin 123` и флаг последним аргументом
    разбирались через раз.
    """
    tg_token = None
    admin_id = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--tg", "-t", "--token"):
            if i + 1 < len(argv):
                tg_token = argv[i + 1]
                i += 2
                continue
        elif arg.startswith("--tg=") or arg.startswith("--token="):
            tg_token = arg.split("=", 1)[1]
        elif arg in ("--admin", "-a"):
            if i + 1 < len(argv):
                try:
                    admin_id = int(argv[i + 1])
                except ValueError:
                    pass
                i += 2
                continue
        elif arg.startswith("--admin="):
            try:
                admin_id = int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg in ("-h", "--help"):
            print("python3 -m br [--tg TOKEN] [--admin ID]")
            print("  --tg TOKEN   запустить управление через Telegram-бота")
            print("  --admin ID   разрешить только этому Telegram ID")
            raise SystemExit(0)
        i += 1
    return tg_token, admin_id


def main():
    tg_token, admin_id = _parse_args(sys.argv[1:])

    if not tg_token:
        tg_token = os.environ.get("TG_BOT_TOKEN")

    if tg_token:
        from br.tg_bot import TelegramInteractiveController

        controller = TelegramInteractiveController(token=tg_token, admin_id=admin_id)
        controller.run()
    else:
        from br.cli import main as cli_main

        cli_main()


if __name__ == "__main__":
    main()