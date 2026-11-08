"""Список серверов игрового проекта.

Раньше модуль знал ровно один проект и один адрес —
api.blackrussia.online/servers.json. Теперь источник берётся у проекта
(projects.py), а здесь остался весь разбор: жёсткий таймаут на запрос,
дисковый кэш, фон-обновление и «сеть не ответила — не долбить её на каждом
аккаунте».

Кэш — отдельный файл на проект. Иначе после переключения на Arizona
оставался список Black Russia и бот шёл на чужие IP.

Имя файла модуля осталось прежним: br_servers импортируют bot.py,
tg_controller.py, web/server.py и тесты. Менять имя — значит ломать их
ради красоты.
"""

import json
import os
import re
import threading
import time
import urllib.request

import projects as projects_mod

# Адреса Black Russia. Оставлены как значения по умолчанию для тех, кто
# зовёт get_servers() без проекта: так старый код и внешние скрипты
# продолжают работать без правок.
API_URLS = list(projects_mod.DEFAULT_PROJECTS["br"]["servers_api"])

# Кэш Black Russia. Тоже ради совместимости: часть скриптов читает
# CACHE_FILE напрямую.
CACHE_FILE = os.path.join(
    projects_mod.BASE_DIR,
    projects_mod.DEFAULT_PROJECTS["br"]["servers_cache_file"],
)

# Жёсткий потолок на один запрос. timeout в urlopen ограничивает отдельный
# recv, а не весь ответ: сервер, отдающий по байту, держал бы нас вечно.
HARD_TIMEOUT = 5.0

CACHE_TTL = 300  # 5 минут кэша

# Когда сеть не отдала список, повторять запрос на КАЖДЫЙ аккаунт нельзя:
# 372 аккаунта по 10 секунд ожидания — это часы. Запоминаем провал и
# следующие вызовы сразу идут в офлайн-кэш.
FAIL_TTL = 120.0

_lock = threading.RLock()
_states: dict = {}


def _state(project) -> dict:
    """Состояние сети и кэша — своё на каждый проект."""
    key = project.key
    with _lock:
        state = _states.get(key)
        if state is None:
            state = {"servers": None, "time": 0.0, "failed_until": 0.0}
            _states[key] = state
        return state


def _project(project=None) -> "projects_mod.Project":
    if isinstance(project, projects_mod.Project):
        return project
    return projects_mod.get_project(project)


def cache_path(project=None) -> str:
    """Путь дискового кэша списка серверов проекта."""
    resolved = _project(project)
    if resolved.key == projects_mod.DEFAULT_PROJECT:
        return CACHE_FILE
    return resolved.servers_cache_path


def api_sources(project=None) -> list:
    """Адреса списка серверов проекта вместе с путём до массива."""
    resolved = _project(project)
    return list(resolved.servers_api)


def api_urls(project=None) -> list:
    """Только адреса — для логов и панели."""
    resolved = _project(project)
    return list(resolved.servers_urls)


def is_configured(project=None) -> bool:
    """Есть ли у проекта вообще откуда взять список серверов."""
    resolved = _project(project)
    return bool(resolved.servers_api or resolved.servers_static)


def reset_cache(project=None) -> None:
    """Сбросить память о списке: переключение проекта, тесты, смена адреса."""
    if project is None:
        with _lock:
            _states.clear()
        return
    with _lock:
        _states.pop(_project(project).key, None)


def _fetch_one(url, deadline):
    """Один запрос в отдельном потоке, с общим дедлайном."""
    box = {}

    def worker():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=max(1.0, deadline)) as resp:
                box["data"] = json.loads(resp.read().decode())
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        return None, TimeoutError(f"{url}: дольше {deadline:.0f}с")
    return box.get("data"), box.get("error")


def _walk(data, path: str):
    """Дойти до узла по пути вида "arizonaMobile" или "data.servers"."""
    node = data
    for piece in str(path or "").split("."):
        piece = piece.strip()
        if not piece:
            continue
        if isinstance(node, dict):
            if piece not in node:
                return None
            node = node[piece]
        elif isinstance(node, list) and piece.lstrip("-").isdigit():
            index = int(piece)
            if not -len(node) <= index < len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def _normalize(data, path: str = ""):
    """Привести ответ API к списку словарей.

    Свой список серверов человек пишет руками в projects.json, и он может
    оказаться и массивом, и объектом по id — как у части API. Разбираем оба
    варианта здесь, а не в каждом вызывающем.

    Путь нужен для чужих форматов: лончер Arizona отдаёт объект с десятком
    разделов (arizona, arizonaMobile, rodinaMobile, ...), и без него мы бы
    взяли первый попавшийся список — например PC-серверы вместо мобильных.
    """
    node = _walk(data, path) if path else data
    if node is None:
        return []
    if isinstance(node, list):
        return [_adapt(item) for item in node if isinstance(item, dict)]
    if isinstance(node, dict):
        for key in ("servers", "data", "list", "query", "result", "items"):
            value = node.get(key)
            if isinstance(value, list):
                return [_adapt(item) for item in value if isinstance(item, dict)]
        by_id = [item for item in node.values() if isinstance(item, dict)]
        if by_id:
            return [_adapt(item) for item in by_id]
    return []


# Разные лончеры называют одни и те же поля по-своему. У Black Russia это
# id/firstname/maxonline, у Arizona — number/name/maxplayers. Приводим к
# одному виду здесь, чтобы весь остальной код знал только канон.
_ID_KEYS = ("id", "number", "num", "sid")
_SHORT_KEYS = ("firstname", "short", "letter", "tag", "key")
_NAME_KEYS = ("name", "title", "hostname", "server_name")
_IP_KEYS = ("ip", "host", "address", "url")
_ONLINE_KEYS = ("online", "players", "playersonline", "online_players")
_MAX_KEYS = ("maxonline", "maxplayers", "max", "slots", "max_players")


def _first_value(item: dict, keys):
    for key in keys:
        value = item.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def _adapt(item: dict) -> dict:
    """Дописать канонические поля, если API проекта зовёт их иначе."""
    out = dict(item)
    for canon, keys in (
        ("id", _ID_KEYS),
        ("firstname", _SHORT_KEYS),
        ("name", _NAME_KEYS),
        ("ip", _IP_KEYS),
        ("online", _ONLINE_KEYS),
        ("maxonline", _MAX_KEYS),
    ):
        if out.get(canon) not in (None, ""):
            continue
        value = _first_value(item, keys)
        if value is not None:
            out[canon] = value
    # Короткое имя показываем, если его нет: у Arizona серверы зовутся
    # «Mobile I», и в списке бота это понятнее пустой строки.
    if out.get("firstname") in (None, ""):
        out["firstname"] = str(out.get("name") or out.get("id") or "").strip()
    # Порт числом: часть API отдаёт его строкой, а socks/сокет ждут int.
    try:
        out["port"] = int(out.get("port") or 0) or out.get("port")
    except (TypeError, ValueError):
        pass
    return out


def _load_disk_cache(project=None):
    path = cache_path(project)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        items = _normalize(data)
        if items:
            return items
    except Exception:
        pass
    return None


def _save_disk_cache(data, project=None):
    path = cache_path(project)
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _static_servers(project):
    resolved = _project(project)
    return [_adapt(dict(item)) for item in resolved.servers_static]


def _refresh_in_background(project):
    resolved = _project(project)
    state = _state(resolved)
    sources = api_sources(resolved)
    if not sources:
        return

    def worker():
        for source in sources:
            data, err = _fetch_one(source.url, HARD_TIMEOUT)
            items = _normalize(data, source.path)
            if items:
                state["servers"] = items
                state["time"] = time.time()
                _save_disk_cache(items, resolved)
                return
        state["failed_until"] = time.time() + FAIL_TTL

    threading.Thread(
        target=worker, daemon=True, name=f"servers-refresh-{resolved.key}"
    ).start()


def _not_configured_error(project) -> RuntimeError:
    return RuntimeError(
        f"список серверов проекта {project.title} не настроен: "
        f"впиши servers_api или servers_static в projects.json"
    )


def get_servers(project=None):
    resolved = _project(project)
    state = _state(resolved)
    now = time.time()
    if state["servers"] and (now - state["time"] < CACHE_TTL):
        return state["servers"]

    # Офлайн-кэш отдаём сразу, а сеть обновляем в фоне. Иначе каждый
    # запуск ждёт 10 секунд на двух URL, и вход в игру выглядит зависшим.
    if state["servers"] is None:
        disk = _load_disk_cache(resolved)
        if disk:
            state["servers"] = disk
            state["time"] = now
            _refresh_in_background(resolved)
            return disk

    # Сеть недавно не ответила — сразу офлайн-кэш, без ожидания.
    if now < state["failed_until"]:
        disk = _load_disk_cache(resolved)
        if disk:
            state["servers"] = disk
            state["time"] = now
            return disk
        static = _static_servers(resolved)
        if static:
            return static
        if not is_configured(resolved):
            raise _not_configured_error(resolved)
        raise RuntimeError("список серверов недоступен, офлайн-кэша нет")

    sources = api_sources(resolved)
    last_err = None
    for source in sources:
        data, err = _fetch_one(source.url, HARD_TIMEOUT)
        items = _normalize(data, source.path)
        if items:
            state["servers"] = items
            state["time"] = now
            _save_disk_cache(items, resolved)
            return items
        last_err = err or last_err

    if state["servers"]:
        return state["servers"]

    state["failed_until"] = now + FAIL_TTL

    disk = _load_disk_cache(resolved)
    if disk:
        state["servers"] = disk
        state["time"] = now
        return disk

    # Свой список серверов — последний рубеж: он не зависит от сети вообще.
    static = _static_servers(resolved)
    if static:
        return static

    if not is_configured(resolved):
        raise _not_configured_error(resolved)

    # Адрес есть, но он мёртв. Для не-BR проекта это чаще всего устаревший
    # URL, а не сеть: подсказываем, где его заменить, а не «сеть легла».
    hint = ""
    if resolved.key != projects_mod.DEFAULT_PROJECT:
        hint = (f" Проверь адрес списка для {resolved.title}: "
                f"projects.json -> projects.{resolved.key}.servers_api")
    raise RuntimeError(f"Не удалось получить список серверов: {last_err}{hint}")


def find_server(query, project=None):
    """Найти сервер по номеру, короткому имени или названию."""
    resolved = _project(project)
    servers = get_servers(resolved)
    q_raw = str(query).strip()
    q_lower = q_raw.lower()

    # 1. Если передано число или есть номер сервера ("1", "01", "сервер 1", "#1", "№1")
    num_match = re.search(r'\d+', q_raw)
    if num_match:
        target_id = int(num_match.group())
        for s in servers:
            if s.get("id") == target_id:
                return s

    # 2. Очищенный поиск по названию (без слова "сервер" и спецсимволов)
    clean_q = re.sub(r'(сервер|server|серв|№|#)', '', q_lower).strip()

    def _variants(item):
        name = str(item.get("name") or "")
        first = str(item.get("firstname") or "")
        return (name.lower(), first.lower(), resolved.clean_server_name(name).lower(),
                resolved.clean_server_name(first).lower())

    # Точное совпадение по имени, короткому имени или названию без префикса
    for s in servers:
        if clean_q and clean_q in _variants(s):
            return s

    # Подстрока
    target = clean_q if clean_q else q_lower
    for s in servers:
        name, first, clean_name, clean_first = _variants(s)
        if any(target in value for value in (name, first, clean_name, clean_first)):
            return s

    return None
