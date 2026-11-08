"""Реальные уровень и опыт бота.

Меню игрока в Black Russia — два диалога: список пунктов и экран данных.
Обработчик диалогов уже умеет пройти оба и разобрать цифры. Здесь —
тонкая обёртка: записать разобранное в поля бота, дождаться свежих
данных по запросу и отдать их панели в готовом виде.

Раньше уровень жил одной строкой вида «3 (2/8)», и каждое место, которому
нужен был опыт до следующего уровня, резало эту строку по скобкам. Теперь
числа лежат отдельно, строка собирается из них.
"""

from __future__ import annotations

import re
import time

# Сколько ждать ответа на /mm. Меню приходит двумя диалогами подряд,
# на живом сервере это 1-3 секунды; 12 — с большим запасом.
REFRESH_TIMEOUT = 12.0


def format_level(level, exp_cur=None, exp_next=None) -> str:
    """Строка для списков: «3 lvl · 12/40» или «3 lvl»."""
    if not level:
        return "-"
    if exp_cur is None or exp_next is None:
        return f"{int(level)} lvl"
    return f"{int(level)} lvl · {int(exp_cur)}/{int(exp_next)}"


def apply(bot, stats: dict) -> dict:
    """Записать разобранную статистику в бота. Возвращает, что изменилось."""
    level = stats.get("level")
    if not level:
        return {"ok": False, "error": "уровень не разобран"}

    bot.level_num = int(level)
    bot.exp_cur = stats.get("exp_cur")
    bot.exp_next = stats.get("exp_next")
    bot.exp_left = stats.get("exp_left")
    bot.level_str = format_level(level, bot.exp_cur, bot.exp_next)
    if stats.get("money") is not None:
        bot.money = int(stats["money"])
    bot.stats_updated_at = time.time()
    return {
        "ok": True,
        "level": bot.level_num,
        "exp_cur": bot.exp_cur,
        "exp_next": bot.exp_next,
        "exp_left": bot.exp_left,
        "level_str": bot.level_str,
    }


def snapshot(bot) -> dict:
    """Поля статистики для панели и Telegram.

    Если числа не разобраны (аккаунт поднят из реестра, где лежит только
    строка), они вытаскиваются из неё: «3 lvl · 12/40». Иначе панель
    показывала бы прочерк рядом с готовой строкой уровня.
    """
    level = getattr(bot, "level_num", None)
    cur = getattr(bot, "exp_cur", None)
    need = getattr(bot, "exp_next", None)
    left = getattr(bot, "exp_left", None)
    text = str(getattr(bot, "level_str", "-") or "-")

    if level is None:
        found = re.search(r"(\d{1,3})", text)
        if found:
            level = int(found.group(1))
    if cur is None or need is None:
        pair = re.search(r"(\d{1,6})\s*/\s*(\d{1,6})", text)
        if pair:
            cur, need = int(pair.group(1)), int(pair.group(2))
            left = max(0, need - cur)

    percent = None
    if cur is not None and need:
        percent = round(min(100.0, max(0.0, cur * 100.0 / need)), 1)
    return {
        "level": text,
        "level_num": level,
        "exp_cur": cur,
        "exp_next": need,
        "exp_left": left,
        "exp_percent": percent,
        "stats_updated_at": getattr(bot, "stats_updated_at", 0.0),
    }


def refresh(bot, *, timeout: float = REFRESH_TIMEOUT, log=None) -> dict:
    """Спросить /mm и дождаться, пока обработчик диалога обновит поля.

    Синхронно ждёт обновления, потому что вызывается из фонового потока:
    панель не должна держать мост, пока бот ходит по меню.
    """
    log = log or (lambda _m: None)
    if not bot.session or not bot.session.connected:
        return {"ok": False, "error": "бот не подключён"}
    if not bot.api:
        return {"ok": False, "error": "нет API бота"}

    before = float(getattr(bot, "stats_updated_at", 0.0) or 0.0)
    bot.stats_pending = True
    bot.stats_pending_at = time.time()
    try:
        bot.api.sendchat("/mm")
    except Exception as exc:
        bot.stats_pending = False
        return {"ok": False, "error": f"чат не отправился: {exc}"}

    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if float(getattr(bot, "stats_updated_at", 0.0) or 0.0) > before:
            return {"ok": True, **snapshot(bot)}
        if not bot.session.connected:
            return {"ok": False, "error": "соединение закрылось"}
        time.sleep(0.25)

    bot.stats_pending = False
    log(f"[СТАТИСТИКА] #{getattr(bot, 'bot_id', '?')}: меню не ответило за {timeout:.0f}с")
    return {"ok": False, "error": "меню статистики не ответило"}
