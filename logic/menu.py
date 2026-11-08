"""Работа с игровыми меню: разбор строк, выбор пункта, вложенные меню.

Меню в Black Russia — это SA-MP диалоги (RPC 347 на вход, 62 на ответ).
Два свойства из-за которых всё ломалось:

1. Номер пункта в тексте и его list_item — РАЗНЫЕ числа.
   В меню игрока строка выглядит как «1. Статистика», но list_item для
   первой строки равен 0. Если отвечать номером из текста, попадаешь на
   строку ниже — в «Список команд» вместо «Статистики».

2. У стилей 4 и 5 первая строка — заголовок колонок, и list_item её НЕ
   считает. У стиля 2 заголовка нет и все строки считаются. Один и тот
   же индекс для разных стилей указывает на разные пункты.

Плюс меню бывают вложенными: выбрал «Статистика» — прилетает ВТОРОЙ
диалог с самими данными. Отвечать на него надо отдельно, иначе он висит
поверх игры и блокирует всё остальное.
"""

from __future__ import annotations

import re
import threading
import time

# SA-MP стили диалогов
STYLE_LIST = 2            # простой список, все строки считаются
STYLE_TABLIST = 4         # список с колонками, без заголовка
STYLE_TABLIST_HEADERS = 5  # список с колонками и заголовком (строка 0 = шапка)

_COLOR_TAG = re.compile(r"\{[0-9a-fA-F]{6}\}")
# «1. », «10. », «1) », «1 - » в начале строки
_NUMBERING = re.compile(r"^\s*\d{1,2}\s*[\.\)\-:]\s*")


def dialog_signature(style, title, info) -> tuple:
    """Отпечаток диалога: стиль и текст. id намеренно не входит.

    В Black Russia все диалоги приходят с id 0, поэтому различать экраны
    приходится по содержимому.
    """
    return (
        int(style or 0),
        strip_colors(title)[:120],
        strip_colors(info)[:400],
    )


def strip_colors(text) -> str:
    return _COLOR_TAG.sub("", str(text or "")).strip()


def strip_numbering(text: str) -> str:
    """Убрать нумерацию из начала строки: «1. Статистика» -> «Статистика»."""
    return _NUMBERING.sub("", strip_colors(text)).strip()


class MenuRow:
    """Одна строка меню: как её видно и какой list_item за неё отвечает."""

    __slots__ = ("index", "text", "raw", "number")

    def __init__(self, index: int, text: str, raw: str, number: int | None = None):
        self.index = index
        self.text = text
        self.raw = raw
        self.number = number

    def __repr__(self):
        return f"MenuRow(index={self.index}, text={self.text!r})"


def parse_rows(info: str, style: int = STYLE_LIST) -> list[MenuRow]:
    """Разобрать строки меню с правильными list_item для стиля.

    Для STYLE_TABLIST_HEADERS первая строка выбрасывается: она заголовок
    колонок и сервер её не считает. Для остальных стилей считаются все.
    """
    raw_rows = [line for line in str(info or "").split("\n") if line.strip()]
    if style == STYLE_TABLIST_HEADERS and raw_rows:
        raw_rows = raw_rows[1:]

    rows: list[MenuRow] = []
    for index, raw in enumerate(raw_rows):
        clean = strip_colors(raw)
        number_match = re.match(r"^\s*(\d{1,2})\s*[\.\)\-:]\s*", clean)
        number = int(number_match.group(1)) if number_match else None
        rows.append(MenuRow(index=index, text=strip_numbering(raw), raw=raw, number=number))
    return rows


def find_row(rows: list[MenuRow], *needles: str, exact: bool = False) -> MenuRow | None:
    """Первая строка, содержащая любую из подстрок (или равная при exact)."""
    wanted = [n.lower() for n in needles if n]
    if not wanted:
        return None
    for row in rows:
        text = row.text.lower()
        for needle in wanted:
            if (text == needle) if exact else (needle in text):
                return row
    return None


def find_row_index(rows: list[MenuRow], *needles: str, exact: bool = False) -> int | None:
    row = find_row(rows, *needles, exact=exact)
    return row.index if row is not None else None


# --- ответы на диалоги ---------------------------------------------------


class MenuAnswerer:
    """Отвечает на диалоги и не даёт ответить на один диалог дважды.

    Сервер присылает меню повторно, если не дождался ответа, а обработчиков
    в проекте несколько (задачи, супервизор спавна, статистика). Без учёта
    уже отвеченных диалогов на один экран уходило по два-три ответа — а это
    и есть «нажатие не туда».
    """

    def __init__(self, log=None, ttl: float = 2.0):
        self.log = log or (lambda _m: None)
        # Ключ — id диалога ПЛЮС отпечаток его содержимого.
        #
        # Только id не годится: в Black Russia почти все диалоги приходят с
        # id 0, и вложенный проход «меню -> статистика» выглядел бы как
        # повторный ответ на тот же диалог. Только содержимое тоже не
        # годится: сервер присылает один и тот же экран повторно, если не
        # дождался ответа, и это надо отличать от нового экрана.
        #
        # Пара (id, отпечаток) решает оба случая: разные экраны отвечаются,
        # повтор того же экрана — нет.
        self.ttl = ttl
        self._answered: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def _key(self, dialog_id: int, signature) -> tuple:
        return (int(dialog_id), signature)

    def note_dialog_shown(self, dialog_id: int) -> None:
        """Сервер прислал диалог заново — на него снова можно ответить."""
        with self._lock:
            for key in [k for k in self._answered if k[0] == int(dialog_id)]:
                self._answered.pop(key, None)

    def already_answered(self, dialog_id: int, signature=None) -> bool:
        now = time.time()
        key = self._key(dialog_id, signature)
        with self._lock:
            stamp = self._answered.get(key)
            if stamp is None:
                return False
            if now - stamp > self.ttl:
                self._answered.pop(key, None)
                return False
            return True

    def mark_answered(self, dialog_id: int, signature=None) -> None:
        with self._lock:
            self._answered[self._key(dialog_id, signature)] = time.time()

    def forget(self, dialog_id: int) -> None:
        with self._lock:
            for key in [k for k in self._answered if k[0] == int(dialog_id)]:
                self._answered.pop(key, None)

    def answer(
        self,
        session,
        dialog_id: int,
        list_item: int = 0,
        response: int = 1,
        text: str = "",
        *,
        force: bool = False,
        note: str = "",
        signature=None,
    ) -> bool:
        """Ответить на диалог. response: 1 = левая кнопка, 0 = правая.

        signature — отпечаток содержимого диалога. Передавайте его всегда,
        когда он известен: без него разные экраны с одним id (а в BR это
        почти все) считаются одним и тем же диалогом.
        """
        if session is None:
            return False
        if not getattr(session, "connected", False):
            return False
        if not force and self.already_answered(dialog_id, signature):
            self.log(
                f"[МЕНЮ] диалог #{dialog_id} уже отвечен только что "
                f"(пункт {list_item}) — второй ответ не шлю"
            )
            return False

        try:
            ok = session.send_dialog_response(
                int(dialog_id), int(response), int(list_item), str(text)
            )
        except Exception as exc:
            self.log(f"[МЕНЮ] ответ на диалог #{dialog_id} не ушёл: {exc}")
            return False

        if ok:
            self.mark_answered(dialog_id, signature)
            self.log(
                f"[МЕНЮ] диалог #{dialog_id}: кнопка={response} "
                f"пункт={list_item} {note}".strip()
            )
        return bool(ok)


# --- известные меню ------------------------------------------------------

# Настоящие заголовки меню. Раньше проверялись «главное меню» и «меню
# персонажа», а сервер пишет «Меню игрока» — поэтому меню /mm не
# распознавалось вообще.
PLAYER_MENU_TITLES = (
    "меню игрока",
    "меню персонажа",
    "главное меню",
)

# Пункт, который открывает статистику персонажа.
STATS_ITEMS = ("статистик", "statistic")

# Признаки того, что диалог — это УЖЕ статистика, а не список пунктов.
STATS_DATA_MARKERS = (
    "уровень",
    "level",
    "опыт",
    "exp",
    "баланс",
    "наличные",
    "банковский",
)


def is_player_menu_title(title: str) -> bool:
    """Заголовок похож на меню игрока.

    Отдельно от наличия пункта «Статистика»: меню может прийти в урезанном
    виде, но это всё равно меню, и его нельзя оставлять висеть.
    """
    title_low = strip_colors(title).lower()
    return any(t in title_low for t in PLAYER_MENU_TITLES)


def looks_like_player_menu(title: str, rows: list[MenuRow]) -> bool:
    """Это меню игрока и в нём есть пункт «Статистика»."""
    if not is_player_menu_title(title):
        return False
    return find_row(rows, *STATS_ITEMS) is not None


def looks_like_stats_data(title: str, info: str) -> bool:
    """Это экран со статистикой? Там есть цифры, а не список пунктов."""
    blob = f"{strip_colors(title)} {strip_colors(info)}".lower()
    return any(marker in blob for marker in STATS_DATA_MARKERS)


def parse_stats(info: str) -> dict:
    """Вытащить уровень, опыт и деньги из экрана статистики.

    Кроме строки «2/8» отдаёт разобранные числа: сколько опыта набрано,
    сколько нужно до следующего уровня и сколько осталось. Без этого
    «2/8» приходится разбирать заново в каждом месте, где показывается
    статистика, и там легко ошибиться.
    """
    clean = strip_colors(info)
    result = {
        "level": None, "exp": None, "money": None, "raw": clean,
        "exp_cur": None, "exp_next": None, "exp_left": None,
    }

    level = re.search(r"(?:уровень|level|lvl)[^\d]{0,12}(\d{1,3})", clean, re.IGNORECASE)
    if level:
        result["level"] = int(level.group(1))

    exp = re.search(
        r"(?:опыт|exp|очки опыта|очков)[^\d]{0,12}(\d{1,6})\s*(?:/|из)\s*(\d{1,6})",
        clean,
        re.IGNORECASE,
    )
    if exp:
        result["exp"] = f"{int(exp.group(1))}/{int(exp.group(2))}"
    else:
        fallback = re.search(r"(\d{1,6})\s*/\s*(\d{1,6})", clean)
        if fallback:
            result["exp"] = f"{int(fallback.group(1))}/{int(fallback.group(2))}"

    if result["exp"]:
        cur_s, next_s = result["exp"].split("/", 1)
        cur, need = int(cur_s), int(next_s)
        result["exp_cur"] = cur
        result["exp_next"] = need
        result["exp_left"] = max(0, need - cur)

    money = re.search(
        r"(?:баланс|деньги|наличные|на руках|банковский счёт|банковский счет)"
        r"[^\d]{0,20}([\d\s,\.]{2,})",
        clean,
        re.IGNORECASE,
    )
    if money:
        digits = re.sub(r"[^\d]", "", money.group(1))
        if digits:
            result["money"] = int(digits)

    return result


# --- решение по диалогу --------------------------------------------------


class Decision:
    """Что сделать с пришедшим диалогом."""

    __slots__ = ("action", "list_item", "response", "note", "row", "screen")

    def __init__(self, action: str, list_item: int = 0, response: int = 1,
                 note: str = "", row: MenuRow | None = None, screen: str = ""):
        self.action = action      # "pick" | "close" | "ignore"
        self.list_item = list_item
        self.response = response
        self.note = note
        self.row = row
        # "menu" — это список пунктов, "stats" — экран с данными.
        # Нужно, чтобы разбор статистики не запускался на самом меню.
        self.screen = screen

    def __repr__(self):
        return (f"Decision({self.action!r}, list_item={self.list_item}, "
                f"response={self.response}, note={self.note!r})")


def decide_stats_flow(
    style: int,
    title: str,
    info: str,
    *,
    stats_pending: bool,
) -> Decision | None:
    """Что делать с диалогом в рамках опроса /mm.

    Возвращает None, если диалог к статистике отношения не имеет — тогда
    его обрабатывают другие обработчики.

    Два экрана:
      1) меню игрока со списком пунктов -> выбрать «Статистика»;
      2) экран с данными -> его разбирает parse_stats, а здесь решается
         только закрытие.

    Порядок важен: меню проверяется ПЕРВЫМ. Иначе слово «Статистика» из
    списка пунктов принимается за экран статистики, и меню закрывается,
    так и не показав данные.

    Меню игрока отвечаем ВСЕГДА, независимо от stats_pending. Раньше здесь
    стоял ранний выход `if not stats_pending: return None`, и /mm, отправленный
    вручную (не по таймеру), оставался без ответа: диалог висел поверх игры,
    сервер на любую следующую команду отвечал «Закройте диалог!».
    """
    rows = parse_rows(info, style)

    # Заголовок проверяем отдельно от наличия пункта: если сервер поменяет
    # состав меню, мы всё равно должны его закрыть, а не оставить висеть.
    if is_player_menu_title(title) and rows:
        stat_row = find_row(rows, *STATS_ITEMS)
        if stat_row is not None:
            return Decision(
                "pick",
                list_item=stat_row.index,
                response=1,
                note=f"меню /mm → «{stat_row.text}»",
                row=stat_row,
                screen="menu",
            )
        return Decision(
            "close",
            list_item=0,
            response=0,
            note="меню /mm: «Статистика» не найдена, закрываю",
            screen="menu",
        )

    if looks_like_stats_data(title, info):
        return Decision(
            "close",
            list_item=0,
            response=0,
            note="статистика: закрываю",
            screen="stats",
        )

    # Дальше — только диалоги, которые нас не касаются. Их трогаем лишь
    # когда статистику действительно запрашивали.
    if not stats_pending:
        return None

    return None
