"""Разбор сообщения о бане: кто забанил, за что и на сколько.

Сервер сообщает о бане двумя разными способами, и они несут разный объём
информации.

Ручной бан администратора — в общий чат, со всеми подробностями:

    Администратор Yaroslav_Kapeka навсегда заблокировал игрока Kyndal_Golodnjak. Причина: BOT
    Администратор Maks_Almazov навсегда заблокировал игрока Juan_Fionin. Причина: BOT | by S. Antipov

Автоматический бан — безлично, ни админа, ни причины:

    Ваш игровой аккаунт заблокирован, вход невозможен.
    Ваш игровой аккаунт был заблокирован администрацией за нарушение

Раньше в Telegram уходила вся строка целиком, и по ней нельзя было понять
ни кто вынес решение, ни за что. Здесь это разбирается на поля, чтобы в
оповещении было видно главное: тип бана, админ, причина, срок.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ник в игре: латиница, цифры, подчёркивание. Бывает в [скобках] — клан-тег.
_NICK = r"[A-Za-z0-9_\[\]{}]{2,24}"

# "Администратор Nick навсегда заблокировал игрока Target. Причина: BOT"
_MANUAL = re.compile(
    rf"(?P<role>администратор|модератор|админ|хелпер|helper|admin)"
    rf"[\s:]+(?P<admin>{_NICK})"
    rf"(?P<middle>.*?)"
    rf"(?:заблокировал|забанил|выдал\s+бан|дал\s+бан)"
    # Падеж бывает разный: "заблокировал игрока X", "выдал бан игроку X".
    rf"(?:\s+игрок[ау]?)?[\s:]+(?P<target>{_NICK})"
    # Хвост берём целиком, вместе с точкой: причина идёт после неё.
    rf"(?P<tail>.*)$",
    re.IGNORECASE,
)

# Срок: "навсегда", "на 7 дней", "на 2 часа", "на 30 минут", "бессрочно"
_DURATION = re.compile(
    r"(?P<forever>навсегда|бессрочно|перманентно|permanent)"
    r"|на\s+(?P<num>\d+)\s*(?P<unit>дн[а-я]*|сут[а-я]*|час[а-я]*|мин[а-я]*|год[а-я]*)",
    re.IGNORECASE,
)

_REASON = re.compile(r"причин[аы][\s:]+(?P<reason>.+?)(?:\s*\||\s*$)", re.IGNORECASE)

# "| by S. Antipov" — кто именно выписал, подпись в конце строки.
_ISSUER = re.compile(r"\|\s*by\s+(?P<issuer>[^\|]+?)\s*$", re.IGNORECASE)

# Блокировка по IP — это НЕ бан аккаунта.
#
# Античит пишет: «Ваш IP-адрес был заблокирован. Если Вы считаете, что
# блокировка была выдана ошибочно, обратитесь в технический раздел».
# Аккаунт при этом цел: с другого адреса он заходит нормально. Но если
# продолжать заходить с того же IP, сервер будет отвечать тем же, и
# выглядеть это будет как «все аккаунты сломались».
#
# Поэтому такой текст надо отличать от бана аккаунта: аккаунт не трогаем,
# а адрес уводим в мусор и меняем на другой.
_IP_BLOCK = re.compile(
    r"(?:"
    # "Ваш IP-адрес был заблокирован", "ip адрес заблокирован"
    r"\bip(?:[\s\-_]*адрес\w*)?\b[^.]{0,40}?(?:заблокирован|забанен|в\s+бане)"
    r"|(?:заблокирован|забанен)[^.]{0,25}?\bip(?:[\s\-_]*адрес\w*)?\b"
    # "блокировка по IP", "запрет по ip"
    r"|(?:блокировк\w*|запрет\w*)\s+(?:по|на)\s+\bip\b"
    r"|\bip\s+(?:заблокирован|в\s+бане|в\s+блокировке)"
    r")",
    re.IGNORECASE,
)

# Автоматический бан: безлично.
_AUTO = re.compile(
    r"(?:ваш\s+)?(?:игровой\s+)?аккаунт\s+(?:был\s+|была\s+)?"
    r"(?:заблокирован|забанен|заблокирована)"
    r"|вход\s+невозможен"
    r"|connection_banned",
    re.IGNORECASE,
)

# Ключевые слова причин, которые сервер пишет по-разному.
_REASON_ALIASES = (
    (("бот", "bot"), "BOT (автоматизация)"),
    (("чит", "cheat", "hack"), "Читы"),
    (("постороннее по", "посторонним по", "стороннее по"), "Постороннее ПО"),
    (("кликер", "clicker", "макрос", "macro"), "Кликер/макрос"),
    (("мультиаккаунт", "мультиакк", "multiaccount"), "Мультиаккаунт"),
    (("флуд", "flood", "спам", "spam"), "Флуд"),
    (("оскорб", "insult", "мат"), "Оскорбления"),
    (("реклам", "advert"), "Реклама"),
    (("обход", "bypass", "багоюз", "buguse"), "Обход/багоюз"),
    (("донат", "donate"), "Донат"),
)


@dataclass
class BanInfo:
    """Разобранное сообщение о бане."""

    kind: str = "unknown"          # manual | auto | unknown
    admin: str = ""                # ник администратора
    role: str = ""                 # администратор / модератор / ...
    reason: str = ""               # причина как её написал сервер
    reason_human: str = ""         # нормализованная причина
    duration: str = ""             # "навсегда", "на 7 дней", ""
    target: str = ""               # кого забанили
    issuer: str = ""               # подпись "| by ..."
    raw: str = ""                  # исходная строка
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "admin": self.admin,
            "role": self.role,
            "reason": self.reason,
            "reason_human": self.reason_human,
            "duration": self.duration,
            "target": self.target,
            "issuer": self.issuer,
            "raw": self.raw,
            "tags": list(self.tags),
        }

    @property
    def summary(self) -> str:
        """Короткая строка для лога и панели."""
        if self.kind == "manual":
            bits = [f"админ {self.admin}" if self.admin else "админ ?"]
            if self.reason_human:
                bits.append(self.reason_human)
            if self.duration:
                bits.append(self.duration)
            return " · ".join(bits)
        if self.kind == "auto":
            return "автобан сервера (без админа и причины)"
        return "причина не распознана"

    @property
    def is_bot_ban(self) -> bool:
        """Бан именно за автоматизацию — на такой имеет смысл искать замену."""
        blob = f"{self.reason} {self.reason_human} {self.raw}".lower()
        return any(w in blob for w in ("бот", "bot", "чит", "кликер", "clicker",
                                       "постороннее по", "автоматизац"))


def is_ip_block(text: str) -> bool:
    """Сервер заблокировал IP, а не аккаунт.

    Проверяем именно связку «ip» и «заблокирован», а не отдельные слова:
    «аккаунт заблокирован» — это бан аккаунта, и путать их нельзя, иначе
    живой аккаунт уйдёт в мусор из-за чужой проблемы с адресом.
    """
    clean = _clean(text)
    if not clean:
        return False
    return bool(_IP_BLOCK.search(clean))


def _clean(text: str) -> str:
    """Убрать цвета SA-MP и лишние пробелы."""
    text = re.sub(r"\{[0-9a-fA-F]{6}\}", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _duration_of(text: str) -> str:
    match = _DURATION.search(text)
    if not match:
        return ""
    if match.group("forever"):
        return "навсегда"
    unit = (match.group("unit") or "").lower()
    num = match.group("num")
    if unit.startswith(("дн", "сут")):
        return f"на {num} дн."
    if unit.startswith("час"):
        return f"на {num} ч."
    if unit.startswith("мин"):
        return f"на {num} мин."
    if unit.startswith("год"):
        return f"на {num} г."
    return f"на {num}"


def _human_reason(reason: str) -> str:
    low = (reason or "").lower()
    for needles, label in _REASON_ALIASES:
        if any(n in low for n in needles):
            return label
    return ""


def parse_ban(text: str) -> BanInfo:
    """Разобрать строку бана. Никогда не бросает — неизвестное вернёт kind=unknown."""
    raw = _clean(text)
    info = BanInfo(raw=raw)
    if not raw:
        return info

    manual = _MANUAL.search(raw)
    if manual:
        info.kind = "manual"
        info.role = (manual.group("role") or "").lower()
        info.admin = (manual.group("admin") or "").strip()
        info.target = (manual.group("target") or "").strip()
        middle = manual.group("middle") or ""
        tail = manual.group("tail") or ""

        info.duration = _duration_of(middle) or _duration_of(tail)

        reason_match = _REASON.search(tail)
        if reason_match:
            info.reason = reason_match.group("reason").strip()
        else:
            # Причина иногда идёт без слова "Причина" — берём хвост как есть.
            info.reason = tail.strip(" .")

        issuer = _ISSUER.search(raw)
        if issuer:
            info.issuer = issuer.group("issuer").strip()
            # Подпись не должна попасть в причину.
            info.reason = _ISSUER.sub("", info.reason).strip(" .")

        info.reason_human = _human_reason(info.reason) or _human_reason(raw)
        info.tags = _tags_of(f"{info.reason} {raw}")
        return info

    if _AUTO.search(raw):
        info.kind = "auto"
        info.reason = raw
        info.reason_human = _human_reason(raw)
        info.duration = _duration_of(raw)
        info.tags = _tags_of(raw)
        return info

    # Строка про бан, но ни один шаблон не подошёл.
    if any(w in raw.lower() for w in ("бан", "блокир")):
        info.kind = "unknown"
        info.reason = raw
        info.reason_human = _human_reason(raw)
        info.duration = _duration_of(raw)
        info.tags = _tags_of(raw)
    return info


def _tags_of(text: str) -> list[str]:
    low = (text or "").lower()
    tags = []
    for needles, label in _REASON_ALIASES:
        if any(n in low for n in needles) and label not in tags:
            tags.append(label)
    return tags
