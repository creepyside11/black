"""Реестр игровых проектов: Black Russia, Arizona RP и всё, что добавят дальше.

Зачем отдельный модуль. Раньше «Black Russia» была не настройкой, а
константой, размазанной по коду: список серверов брался с
api.blackrussia.online, суффикс HTTP-пинга был прибит к blackrussia.online,
реестр аккаунтов — accounts.json, а из названия сервера срезалась строка
"BLACK RUSSIA | ". Второй проект на такой базе не поднять: правка в одном
месте ломала первый.

Теперь всё, что отличает один проект от другого, лежит здесь. Модуль знает
только про данные: ни сети, ни потоков, ни зависимостей от bot.py. Список
серверов читает br_servers.py, аккаунты — BotManager, меню — Telegram и
веб-панель, и все они спрашивают проект у этого модуля.

Файл projects.json рядом с accounts.json — то же самое правкой без кода.
Приоритет: переменная окружения -> projects.json -> значения по умолчанию
отсюда. Так настоящий адрес API Arizona вписывается одной строкой, а если
файла нет — проект всё равно поднимается.

Переменные окружения:
    BR_PROJECTS_FILE   — путь к projects.json (тесты и нестандартные сборки)
    BR_PROJECT         — какой проект считать активным на этот запуск
    <ID>_SERVERS_URL   — подменить адрес списка серверов (ARZ_SERVERS_URL)
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get(
    "BR_PROJECTS_FILE", os.path.join(BASE_DIR, "projects.json")
)

# Проект по умолчанию: без него не поднялся бы ни один существующий
# аккаунт — все записи в accounts.json принадлежат Black Russia.
DEFAULT_PROJECT = "br"


@dataclass(frozen=True)
class ServerSource:
    """Один адрес списка серверов и путь до массива внутри ответа.

    У Black Russia ответ — сразу массив. У Arizona лончер отдаёт объект с
    десятком разделов (arizona, arizonaMobile, rodinaMobile, ...), и нужный
    массив лежит по ключу. Без пути разбор молча вернул бы пустой список —
    ровно этим и опасен чужой формат.
    """

    url: str
    path: str = ""

    def as_dict(self) -> dict:
        return {"url": self.url, "path": self.path} if self.path else {"url": self.url}


@dataclass(frozen=True)
class Project:
    """Один игровой проект со всеми своими адресами и правилами."""

    key: str
    title: str
    short: str
    # Откуда брать список серверов. Порядок важен: первый живой ответ выигрывает.
    servers_api: tuple[ServerSource, ...] = ()
    # Запасной список, когда сеть недоступна: [{"name":..., "ip":..., "port":...}]
    servers_static: tuple[dict, ...] = ()
    # Файлы данных: у каждого проекта свой реестр, чтобы фермы не смешивались
    # и переключение проекта не переписывало чужой accounts.json.
    accounts_file: str = ""
    servers_cache_file: str = ""
    # Суффикс Host для HTTP pre-ping: клиент стучится именем сервера в зоне проекта.
    http_host_suffix: str = ""
    # Максимальная длина ника в игре.
    nickname_max: int = 20
    # Что срезать с названия сервера для короткого имени ("BLACK RUSSIA | RED").
    name_prefixes: tuple[str, ...] = ()
    # Каким пакетом начинается вход. "br" — два байта b"BR" и свой
    # OPEN_CONNECTION_REPLY (Black Russia). "raknet" — стандартный
    # offline-обмен RakNet 0x05/0x06/0x07/0x08/0x09, как в SA-MP и RakSAMP.
    handshake: str = "br"
    # Профиль с id RPC и интерфейсов, если сервер разошёлся с Black Russia.
    rpc_profile: str = ""
    # Что сказать человеку в меню: отличия проекта, которые не выразить полями.
    note: str = ""
    # Во что развернулись относительные пути (для тестов подменяется).
    base_dir: str = field(default="", compare=False)

    # --- удобства ---

    @property
    def accounts_path(self) -> str:
        return os.path.join(self.base_dir or BASE_DIR, self.accounts_file)

    @property
    def servers_cache_path(self) -> str:
        return os.path.join(self.base_dir or BASE_DIR, self.servers_cache_file)

    @property
    def rpc_profile_path(self) -> str:
        if not self.rpc_profile:
            return ""
        return os.path.join(self.base_dir or BASE_DIR, self.rpc_profile)

    @property
    def servers_urls(self) -> tuple[str, ...]:
        """Только адреса — для логов, панели и совместимости со старым кодом."""
        return tuple(source.url for source in self.servers_api)

    def clean_server_name(self, raw: str) -> str:
        """Имя сервера без префикса проекта — как его показывает бот."""
        name = str(raw or "").strip()
        for prefix in self.name_prefixes:
            if name.upper().startswith(prefix.upper()):
                name = name[len(prefix):].strip()
                break
        return name or str(raw or "").strip()

    def as_dict(self) -> dict:
        """Представление для панели и Telegram: без путей и служебных полей."""
        return {
            "key": self.key,
            "title": self.title,
            "short": self.short,
            "host_suffix": self.http_host_suffix,
            "nickname_max": self.nickname_max,
            "handshake": self.handshake,
            "has_servers_api": bool(self.servers_api),
            "servers_api": [source.as_dict() for source in self.servers_api],
            "accounts_file": self.accounts_file,
            "note": self.note,
        }


# --- значения по умолчанию ------------------------------------------------
#
# У Arizona свой формат списка: лончер отдаёт объект с разделами, и мобильные
# серверы лежат в arizonaMobile. Путь указывается рядом с адресом — если
# Arizona переименует раздел, правка одна строка в projects.json. Второй
# адрес — пинг мобильного клиента, он остаётся как резерв.

DEFAULT_PROJECTS: dict[str, dict] = {
    "br": {
        "title": "Black Russia",
        "short": "BR",
        "servers_api": [
            "http://api.blackrussia.online/servers.json",
            "https://api.blackrussia.online/servers.json",
        ],
        "servers_static": [],
        "accounts_file": "accounts.json",
        "servers_cache_file": ".servers_cache.json",
        "http_host_suffix": "blackrussia.online",
        "nickname_max": 20,
        "name_prefixes": ["BLACK RUSSIA | ", "BLACK RUSSIA|", "BLACK RUSSIA "],
        "rpc_profile": "",
        "handshake": "br",
        "note": "текущий проект",
    },
    "arz": {
        "title": "Arizona RP",
        "short": "ARZ",
        # Первым идёт основной API лончера: в нём разделы по проектам, и
        # мобильные серверы лежат в arizonaMobile. Второй адрес — пинг
        # мобильного клиента, формат {"query": [...]}: остаётся резервом на
        # случай, если лончерный API переедет. api.arizona-rp.com за
        # Cloudflare отдаёт 1014 на все пути — он тут не используется.
        # Список руками: у лончера Arizona мобильные серверы лежат вперемешку
        # с PC-серверами, а ping-эндпоинт отдавал mobile1/2/3 с портом 5125.
        # 5125 — не игровой порт: UDP там открыт, но на хендшейк никто не
        # отвечает, а на 7777 сервер живёт. Источник один — этот список;
        # адреса и номера дал владелец проекта.
        "servers_api": [],
        "servers_static": [
            {"id": 1, "name": 'PHOENIX', "ip": '185.169.134.3', "port": 7777},
            {"id": 2, "name": 'TUCSON', "ip": '185.169.134.4', "port": 7777},
            {"id": 3, "name": 'SCOTTDALE', "ip": '185.169.134.43', "port": 7777},
            {"id": 4, "name": 'CHANDLER', "ip": '185.169.134.44', "port": 7777},
            {"id": 5, "name": 'BRAINBURG', "ip": '185.169.134.45', "port": 7777},
            {"id": 6, "name": 'SAINT ROSE', "ip": '185.169.134.5', "port": 7777},
            {"id": 7, "name": 'MESA', "ip": '185.169.134.59', "port": 7777},
            {"id": 8, "name": 'RED ROCK', "ip": '185.169.134.61', "port": 7777},
            {"id": 9, "name": 'YUMA', "ip": '185.169.134.107', "port": 7777},
            {"id": 10, "name": 'SURPRISE', "ip": '185.169.134.109', "port": 7777},
            {"id": 11, "name": 'PRESCOTT', "ip": '185.169.134.166', "port": 7777},
            {"id": 12, "name": 'GLENDALE', "ip": '185.169.134.171', "port": 7777},
            {"id": 13, "name": 'KINGMAN', "ip": '185.169.134.172', "port": 7777},
            {"id": 14, "name": 'WINSLOW', "ip": '185.169.134.173', "port": 7777},
            {"id": 15, "name": 'PAYSON', "ip": '185.169.134.174', "port": 7777},
            {"id": 16, "name": 'GILBERT', "ip": '80.66.82.191', "port": 7777},
            {"id": 17, "name": 'SHOW-LOW', "ip": '80.66.82.190', "port": 7777},
            {"id": 18, "name": 'CASA GRANDE', "ip": '80.66.82.188', "port": 7777},
            {"id": 19, "name": 'PAGE', "ip": '80.66.82.168', "port": 7777},
            {"id": 20, "name": 'SUN CITY', "ip": '80.66.82.159', "port": 7777},
            {"id": 21, "name": 'QUEEN CREEK', "ip": '80.66.82.200', "port": 7777},
            {"id": 22, "name": 'SEDONA', "ip": '80.66.82.144', "port": 7777},
            {"id": 23, "name": 'HOLIDAY', "ip": '80.66.82.132', "port": 7777},
            {"id": 24, "name": 'WEDNESDAY', "ip": '80.66.82.128', "port": 7777},
            {"id": 25, "name": 'YAVA', "ip": '80.66.82.113', "port": 7777},
            {"id": 26, "name": 'FARAWAY', "ip": '80.66.82.82', "port": 7777},
            {"id": 27, "name": 'BUMBLE BEE', "ip": '80.66.82.87', "port": 7777},
            {"id": 28, "name": 'CHRISTMAS', "ip": '80.66.82.54', "port": 7777},
            {"id": 29, "name": 'MIRAGE', "ip": '80.66.82.39', "port": 7777},
            {"id": 30, "name": 'LOVE', "ip": '80.66.82.33', "port": 7777},
            {"id": 31, "name": 'DRAKE', "ip": '80.66.82.22', "port": 7777},
            {"id": 32, "name": 'SPACE', "ip": '80.66.82.199', "port": 7777},
        ],
        "accounts_file": "accounts_arz.json",
        "servers_cache_file": ".servers_cache_arz.json",
        # Пустой суффикс — не оплошность. У Black Russia серверы зовутся
        # RED/GREEN, и Host собирается как red.blackrussia.online. У Arizona
        # в списке уже готовое имя (mobile3.arizona-rp.com), и приклеенный
        # суффикс давал «mobile iii.arizona-rp.com» — такого домена нет,
        # pre-ping уходил в никуда, а сервер после этого молчит на
        # UDP-хендшейк. С пустым суффиксом Host равен адресу сервера.
        "http_host_suffix": "",
        "nickname_max": 20,
        "name_prefixes": ["ARIZONA RP | ", "ARIZONA | ", "ARIZONA RP|"],
        "rpc_profile": "projects/arz.profile.json",
        # Arizona — SA-MP-форк (libsamp.so), а не CRMP: пакета b"BR", с
        # которого начинается вход в Black Russia, у неё нет. Вход настроен
        # на стандартный offline-обмен RakNet (0x05 -> 0x06 -> 0x07 -> 0x08).
        #
        # Статус на 02.11: вход не подтверждён. Игровой порт — 7777 (не 5125,
        # который отдавал лончер: на нём открыт UDP, но хендшейка там нет).
        # Ни один из 32 живых серверов не ответил ни на REQUEST_1 в любых
        # вариантах (версии 1..16, с magic и без, 18 и 1400 байт), ни на
        # BR-формат 0x04+"BR", ни на перебор id+маркер.
        #
        # Отдельно: та же проба не получила ответа и от чужих SA-MP серверов
        # с онлайном 800-1200, то есть наш REQUEST_1 сам по себе не проверен
        # на рабочем эталоне. Пока эталон не поднят, «сервер молчит» ничего
        # не доказывает — чинить надо сначала хендшейк, потом смотреть вход.
        #   python3 tools/project_handshake.py arz    # проверить ещё раз
        # Пока формат не разобран живьём, вход на Arizona не работает.
        "handshake": "raknet",
        "note": "32 сервера/порт 7777; вход не подтверждён — формат первого пакета не разобран",
    },
}

# Как человек может назвать проект в команде. Ключи — то, что он напишет.
ALIASES: dict[str, str] = {
    "br": "br",
    "brs": "br",
    "blackrussia": "br",
    "black-russia": "br",
    "black russia": "br",
    "black": "br",
    "бр": "br",
    "блек": "br",
    "блекраша": "br",
    "чернаяроссия": "br",
    "arz": "arz",
    "arizona": "arz",
    "arizona-rp": "arz",
    "arizona rp": "arz",
    "arizonarp": "arz",
    "az": "arz",
    "аризона": "arz",
    "аризонарп": "arz",
    "аризона рп": "arz",
}

_lock = threading.RLock()
_cache: dict | None = None
# Настройки offline-хендшейка RakNet из профиля проекта.
_raknet_options: dict = {}
_cache_stamp: tuple | None = None

# Значения id до применения профилей. Снимаются при первом применении: id
# принадлежат samp/constants.py, и тащить этот импорт на старте модуля ради
# снимка нельзя — projects.py должен грузиться и без пакета.
_CONST_DEFAULTS: dict = {}


def _restore_defaults(cls_name: str, target) -> None:
    """Вернуть id класса к исходным, чтобы профиль не наслаивался на профиль."""
    with _lock:
        snapshot = _CONST_DEFAULTS.get(cls_name)
        if snapshot is None:
            snapshot = {
                name: value
                for name, value in vars(target).items()
                if not name.startswith("_") and isinstance(value, int)
            }
            _CONST_DEFAULTS[cls_name] = snapshot
        for name, value in snapshot.items():
            setattr(target, name, value)


def _env_servers_url(key: str) -> list:
    raw = os.environ.get(f"{key.upper()}_SERVERS_URL", "").strip()
    if not raw:
        return []
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


def _read_file() -> dict:
    """Прочитать projects.json. Битый файл — не повод падать.

    Реестр проектов правит человек руками, и запятая не там не должна
    оставлять его без бота: файл игнорируется, работает встроенный набор.
    """
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _stamp():
    try:
        st = os.stat(CONFIG_FILE)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _raw_config() -> dict:
    """Конфиг с учётом правок на диске: файл мог поменять сам человек."""
    global _cache, _cache_stamp
    stamp = _stamp()
    with _lock:
        if _cache is not None and stamp == _cache_stamp:
            return _cache
        data = _read_file()
        merged: dict = {"active": DEFAULT_PROJECT, "projects": {}}
        for key, defaults in DEFAULT_PROJECTS.items():
            merged["projects"][key] = dict(defaults)
        file_projects = data.get("projects")
        if isinstance(file_projects, dict):
            for key, override in file_projects.items():
                if not isinstance(override, dict):
                    continue
                base = dict(merged["projects"].get(key) or {})
                base.update(override)
                merged["projects"][key] = base
        active = str(data.get("active") or "").strip()
        if active:
            merged["active"] = active
        env_active = os.environ.get("BR_PROJECT", "").strip()
        if env_active:
            merged["active"] = env_active
        _cache = merged
        _cache_stamp = stamp
        return merged


def _sources(value) -> tuple:
    """Разобрать servers_api: строка или {"url":..., "path":...}.

    Так один и тот же список может содержать и простой адрес Black Russia,
    и адрес Arizona с путём до массива внутри ответа.
    """
    if isinstance(value, (str, bytes, dict)):
        value = [value]
    out = []
    for item in value or ():
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("api") or "").strip()
            path = str(item.get("path") or "").strip().strip(".")
            if url:
                out.append(ServerSource(url=url, path=path))
            continue
        text = str(item or "").strip()
        if text:
            out.append(ServerSource(url=text))
    return tuple(out)


def _build(key: str, raw: dict) -> Project:
    def _list(name: str) -> tuple:
        value = raw.get(name) or ()
        if isinstance(value, (str, bytes)):
            value = [value]
        out = []
        for item in value:
            if name == "servers_static":
                if isinstance(item, dict) and (item.get("ip") or item.get("url")):
                    out.append(dict(item))
            elif str(item).strip():
                out.append(str(item).strip())
        return tuple(out)

    def _int(name: str, fallback: int) -> int:
        try:
            value = int(raw.get(name, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    def _handshake(value) -> str:
        """Только два известных значения. Опечатка в projects.json вернула
        бы поток в BR-режим молча, и понять это по логам было бы нельзя."""
        text = str(value or "br").strip().lower()
        return text if text in ("br", "raknet") else "br"

    # Переменная окружения подменяет адрес целиком: нужна, когда настоящий
    # URL проверяют, не трогая файл. Путь в этом случае не задаётся —
    # сработает авторазбор ответа.
    env_urls = _env_servers_url(key)
    api = _sources(env_urls) if env_urls else _sources(raw.get("servers_api"))
    return Project(
        key=key,
        title=str(raw.get("title") or key.upper()),
        short=str(raw.get("short") or key.upper())[:6],
        servers_api=api,
        servers_static=_list("servers_static"),
        accounts_file=str(raw.get("accounts_file") or f"accounts_{key}.json"),
        servers_cache_file=str(
            raw.get("servers_cache_file") or f".servers_cache_{key}.json"
        ),
        http_host_suffix=str(raw.get("http_host_suffix") or "").strip().strip("."),
        nickname_max=_int("nickname_max", 20),
        name_prefixes=_list("name_prefixes"),
        rpc_profile=str(raw.get("rpc_profile") or "").strip(),
        handshake=_handshake(raw.get("handshake")),
        note=str(raw.get("note") or ""),
        base_dir=BASE_DIR,
    )


def reset_cache() -> None:
    """Сбросить кэш конфига: правка projects.json руками и тесты."""
    global _cache, _cache_stamp, _raknet_options
    with _lock:
        _cache = None
        _cache_stamp = None
        # Настройки хендшейка принадлежат профилю проекта: после сброса они
        # перечитываются заново, иначе новому проекту достались бы чужие.
        _raknet_options = {}


def all_projects() -> dict:
    raw = _raw_config()
    projects = {}
    for key, item in (raw.get("projects") or {}).items():
        if isinstance(item, dict):
            projects[str(key)] = _build(str(key), item)
    if not projects:
        projects = {k: _build(k, v) for k, v in DEFAULT_PROJECTS.items()}
    return projects


def project_keys() -> list:
    return list(all_projects().keys())


def resolve_key(raw) -> str | None:
    """Привести то, что написал человек, к ключу проекта.

    Возвращает None, если не узнали: вызывающий сам решает, ругаться или
    взять текущий проект. Молча подставлять Black Russia нельзя — иначе
    «arizona» с опечаткой уедет в чужую ферму.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    text = text.replace("_", " ").replace(".", " ").strip()
    text = " ".join(text.split())
    projects = all_projects()
    if text in projects:
        return text
    if text in ALIASES and ALIASES[text] in projects:
        return ALIASES[text]
    compact = text.replace(" ", "").replace("-", "")
    if compact in projects:
        return compact
    for alias, key in ALIASES.items():
        if key in projects and alias.replace(" ", "").replace("-", "") == compact:
            return key
    # Последняя попытка: начало названия («ариз», «black ru»).
    for key, project in projects.items():
        for candidate in (project.key, project.title.lower(), project.short.lower()):
            if candidate and (candidate.startswith(text) or text.startswith(candidate)):
                return key
    return None


def active_key() -> str:
    raw = _raw_config()
    key = str(raw.get("active") or DEFAULT_PROJECT)
    projects = all_projects()
    if key in projects:
        return key
    resolved = resolve_key(key)
    if resolved:
        return resolved
    return DEFAULT_PROJECT if DEFAULT_PROJECT in projects else next(iter(projects))


def active_project() -> Project:
    return all_projects()[active_key()]


def get_project(raw=None) -> Project:
    """Проект по ключу или алиасу. Без аргумента — активный.

    Неизвестный ключ — не исключение: сюда попадают строки из CLI и из
    старых аккаунтов, и падать на них посреди запуска незачем.
    """
    text = str(raw or "").strip()
    if not text:
        return active_project()
    projects = all_projects()
    if text in projects:
        return projects[text]
    key = resolve_key(text)
    if key and key in projects:
        return projects[key]
    return active_project()


def set_active(raw) -> Project:
    """Переключить активный проект и сохранить выбор на диск."""
    projects = all_projects()
    key = raw if raw in projects else resolve_key(raw)
    if not key or key not in projects:
        raise KeyError(f"неизвестный проект: {raw}")
    _write_active(key)
    reset_cache()
    return all_projects()[key]


def _write_active(key: str) -> None:
    """Записать активный проект, не потеряв ручные правки projects.json.

    Файла нет — создаём с полным набором проектов: человеку должно быть что
    править, а не пустое {"active": ...}.
    """
    payload = _read_file()
    payload["active"] = key
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        payload["projects"] = projects
    for name, defaults in DEFAULT_PROJECTS.items():
        if not isinstance(projects.get(name), dict):
            projects[name] = dict(defaults)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_FILE)


def describe() -> list:
    """Список проектов для меню: ключ, название и пометка активного."""
    current = active_key()
    out = []
    for key, project in all_projects().items():
        item = project.as_dict()
        item["active"] = key == current
        out.append(item)
    return out


def apply_rpc_profile(project: Project | None = None, *, log=None) -> dict:
    """Наложить профиль проекта на id RPC и интерфейсов.

    Серверные скрипты у проектов свои: у Black Russia ShowDialog — это
    интерфейс 347, а ответ на диалог — RPC 406. У Arizona нумерация может
    быть другой, и гадать по ней нельзя — только замер на живом сервере.
    Поэтому id не зашиты, а переопределяются файлом профиля:

        {"rpc": {"MENU_SELECT": 343}, "interfaces": {"AUTH": 38}}

    Возвращает то, что реально применилось, — это уходит в лог запуска,
    чтобы «почему бот молчит на Arizona» разбиралось по фактам.
    """
    project = project or active_project()
    path = project.rpc_profile_path
    report: dict = {"project": project.key, "path": path, "applied": {}}
    if not path or not os.path.exists(path):
        return report
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        report["error"] = f"{exc!r}"
        if log:
            log(f"[ПРОЕКТ] профиль {path} не прочитан: {exc!r}")
        return report
    if not isinstance(data, dict):
        return report

    try:
        from samp import constants as constants_mod
    except Exception as exc:  # pragma: no cover — импорт вне пакета
        report["error"] = f"{exc!r}"
        return report

    applied: dict = {}
    for section, cls_name in (("rpc", "RPC"), ("interfaces", "InterfaceID"),
                              ("packets", "PacketID")):
        values = data.get(section)
        target = getattr(constants_mod, cls_name, None)
        if target is None:
            continue
        # Сначала откатываем прошлый профиль. Без этого id, поправленные для
        # Arizona, остались бы в силе после возврата на Black Russia: профиль
        # BR пустой, и переписывать их обратно было бы нечем.
        _restore_defaults(cls_name, target)
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if not hasattr(target, str(name)):
                continue
            try:
                setattr(target, str(name), int(value))
            except (TypeError, ValueError):
                continue
            applied[f"{cls_name}.{name}"] = int(value)
    report["applied"] = applied

    # Настройки входа для стандартного RakNet. Версия протокола и наличие
    # magic у разных сборок разные, и если замер показал не 5/без magic —
    # это правится файлом, а не кодом. Нет секции — настройки сбрасываются:
    # иначе после Arizona они остались бы в силе на Black Russia.
    global _raknet_options
    options = data.get("raknet")
    clean: dict = {}
    if isinstance(options, dict):
        try:
            version = int(options.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        if 0 < version < 256:
            clean["version"] = version
        if "magic" in options:
            clean["magic"] = bool(options.get("magic"))
    with _lock:
        _raknet_options = clean
    report["raknet"] = clean

    if applied and log:
        items = ", ".join(f"{k}={v}" for k, v in sorted(applied.items()))
        log(f"[ПРОЕКТ] {project.title}: применены id из профиля — {items}")
    return report


def raknet_options() -> dict:
    """Версия протокола и magic для offline-хендшейка RakNet.

    Пусто — значит сборка по умолчанию (SA-MP: версия 5, без magic).
    """
    with _lock:
        return dict(_raknet_options)


def default_project() -> Project:
    """Проект по умолчанию по имени (совместимость со старым кодом)."""
    projects = all_projects()
    return projects.get(DEFAULT_PROJECT) or next(iter(projects.values()))


__all__ = [
    "Project",
    "DEFAULT_PROJECTS",
    "DEFAULT_PROJECT",
    "all_projects",
    "active_key",
    "active_project",
    "apply_rpc_profile",
    "default_project",
    "describe",
    "get_project",
    "project_keys",
    "raknet_options",
    "reset_cache",
    "resolve_key",
    "set_active",
]
