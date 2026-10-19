"""Матрица вариантов ответа на меню фермы.

Зачем это отдельным модулем. Меню приходит SA-MP диалогом (RPC 347), ответ
уходит RPC 62 — стандартная пара, и она заведомо верна. Значит молчание
сервера означает не «пакет не дошёл», а «сервер не нашёл такой пункт»:
в SA-MP обработчик выглядит как `if (response) switch (listitem)`, и при
несуществующем индексе он просто ничего не делает. Ни ошибки, ни закрытия.

Отсюда два вопроса, которые надо разделить экспериментом:

    1. Доходит ли ответ вообще?
    2. Если доходит — какой list_item сервер считает пунктом #3?

Контрольный опыт для (1) — ответ с response=0 (кнопка «Закрыть»). Если меню
закрылось, доставка и id верны, и остаётся только подобрать индекс. Если не
закрылось — проблема в транспорте, и перебирать индексы бессмысленно.

Модуль только собирает варианты и разбирает результат. Ничего не отправляет
сам: отправку делает тот, у кого есть живая сессия.
"""

from __future__ import annotations

# Кнопки диалога в SA-MP: 1 — первая, 0 — вторая.
RESPONSE_BUTTON1 = 1
RESPONSE_BUTTON2 = 0

# Пункт «Начать / Закончить работу» — третий по счёту в меню.
TARGET_ITEM_NUMBER = 3


def dialog_rows(info: str) -> list[str]:
    """Строки диалога как их видит игрок."""
    return [line for line in str(info or "").split("\n") if line.strip()]


def build_variants(rows: int = 4) -> list[dict]:
    """Все осмысленные варианты ответа, по порядку проверки.

    Каждый вариант — словарь с полями:
        name     короткое имя для лога
        response кнопка (1 или 0)
        item     list_item
        why      что именно проверяет
    """
    variants: list[dict] = []

    # Контроль: закрытие. Проверяет доставку, а не индекс.
    variants.append({
        "name": "close",
        "response": RESPONSE_BUTTON2,
        "item": 0,
        "why": "контроль доставки: меню должно закрыться",
    })

    # Заголовок не считается -> #3 это индекс 2.
    variants.append({
        "name": "idx2",
        "response": RESPONSE_BUTTON1,
        "item": 2,
        "why": "TABLIST_HEADERS: заголовок не в счёте",
    })

    # Заголовок считается -> #3 это индекс 3.
    variants.append({
        "name": "idx3",
        "response": RESPONSE_BUTTON1,
        "item": 3,
        "why": "заголовок тоже строка списка",
    })

    # Соседние индексы: если сервер нумерует иначе, попадём хотя бы в #2/#4
    # и увидим по тексту ответа, куда именно мы попали.
    for item in (1, 4, 0, 5):
        if item >= rows + 2:
            continue
        variants.append({
            "name": f"idx{item}",
            "response": RESPONSE_BUTTON1,
            "item": item,
            "why": "соседний пункт: по ответу видно, какой индекс чему равен",
        })

    return variants


def classify(after: dict | None) -> str:
    """Разобрать исход одной попытки по ВСЕМ каналам.

    after — что пришло после ответа:
        None или пустой словарь — тишина
        {"dialog": {"title":..., "info":...}}
        {"json": (iid, payload)}
        {"chat": [строки]}

    Реакция сервера не обязана быть диалогом: устройство на работу легко
    может ответить строкой в чат или уведомлением iid=13. Раньше проба
    смотрела только на диалоги и объявляла «тишину» там, где на самом деле
    всё сработало.
    """
    if not after:
        return "silence"

    dialog = after.get("dialog")
    if dialog:
        title = str(dialog.get("title") or "")
        info = str(dialog.get("info") or "")
        if title or info:
            return "follow_up"
        return "delivered_closed"

    if after.get("json"):
        return "follow_up"

    if after.get("chat"):
        return "chat"

    return "silence"


def summarise(attempts: list[dict]) -> str:
    """Собрать отчёт по всем попыткам.

    attempts — список словарей с полями name, item, response, outcome,
    follow_up (заголовок пришедшего диалога) и why.
    """
    if not attempts:
        return "попыток не было"

    lines = ["вариант      item  resp  исход             что пришло"]
    lines.append("-" * 76)
    for attempt in attempts:
        detail = (attempt.get("detail") or "")[:32]
        lines.append(
            f"{attempt['name']:<12} {attempt['item']:>4}  {attempt['response']:>4}  "
            f"{attempt['outcome']:<16} {detail}"
        )

    lines.append("")
    delivered = any(a["outcome"] == "delivered_closed" for a in attempts)
    found = [a for a in attempts if a["outcome"] in ("follow_up", "chat")]

    if found:
        best = found[0]
        lines.append(
            f"РАБОЧИЙ ВАРИАНТ: list_item={best['item']}, response={best['response']} "
            f"({best['name']}), реакция: {best['outcome']}"
        )
    elif delivered:
        lines.append(
            "Доставка верна (меню закрывается), но ни один индекс не дал реакции "
            "ни в диалоге, ни в чате, ни в JSON. Значит пункт #3 обрабатывается "
            "не через list_item."
        )
    else:
        lines.append(
            "Не закрылось даже кнопкой «Закрыть» — проблема в транспорте, "
            "а не в индексе. Перебирать индексы дальше бессмысленно."
        )
    return "\n".join(lines)
