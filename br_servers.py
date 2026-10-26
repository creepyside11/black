import json
import os
import re
import threading
import time
import urllib.request

API_URLS = [
    "http://api.blackrussia.online/servers.json",
    "https://api.blackrussia.online/servers.json"
]

# Кэш на диске: без него при недоступном API каждый запуск висел на сети,
# а load_accounts отбрасывал ВСЕ аккаунты как «неизвестный сервер».
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".servers_cache.json")

# Жёсткий потолок на один запрос. timeout в urlopen ограничивает отдельный
# recv, а не весь ответ: сервер, отдающий по байту, держал бы нас вечно.
HARD_TIMEOUT = 5.0

_CACHED_SERVERS = None
_CACHE_TIME = 0
CACHE_TTL = 300  # 5 минут кэша

# Когда сеть не отдала список, повторять запрос на КАЖДЫЙ аккаунт нельзя:
# 372 аккаунта по 10 секунд ожидания — это часы. Запоминаем провал и
# следующие вызовы сразу идут в офлайн-кэш.
_FAILED_UNTIL = 0.0
FAIL_TTL = 120.0


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


def _load_disk_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def _save_disk_cache(data):
    try:
        tmp = f"{CACHE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _refresh_in_background():
    """Обновить список из сети, не задерживая вызывающего."""
    def worker():
        global _CACHED_SERVERS, _CACHE_TIME, _FAILED_UNTIL
        last_err = None
        for url in API_URLS:
            data, err = _fetch_one(url, HARD_TIMEOUT)
            if data:
                _CACHED_SERVERS = data
                _CACHE_TIME = time.time()
                _save_disk_cache(data)
                return
            last_err = err or last_err
        _FAILED_UNTIL = time.time() + FAIL_TTL

    threading.Thread(target=worker, daemon=True, name="servers-refresh").start()


def get_servers():
    global _CACHED_SERVERS, _CACHE_TIME, _FAILED_UNTIL
    now = time.time()
    if _CACHED_SERVERS and (now - _CACHE_TIME < CACHE_TTL):
        return _CACHED_SERVERS

    # Офлайн-кэш отдаём сразу, а сеть обновляем в фоне. Иначе каждый
    # запуск ждёт 10 секунд на двух URL, и вход в игру выглядит зависшим.
    if _CACHED_SERVERS is None:
        disk = _load_disk_cache()
        if disk:
            _CACHED_SERVERS = disk
            _CACHE_TIME = now
            _refresh_in_background()
            return disk

    # Сеть недавно не ответила — сразу офлайн-кэш, без ожидания.
    if now < _FAILED_UNTIL:
        disk = _load_disk_cache()
        if disk:
            _CACHED_SERVERS = disk
            _CACHE_TIME = now
            return disk
        raise RuntimeError("список серверов недоступен, офлайн-кэша нет")

    last_err = None
    for url in API_URLS:
        data, err = _fetch_one(url, HARD_TIMEOUT)
        if data:
            _CACHED_SERVERS = data
            _CACHE_TIME = now
            _save_disk_cache(data)
            return data
        last_err = err or last_err

    if _CACHED_SERVERS:
        return _CACHED_SERVERS

    _FAILED_UNTIL = now + FAIL_TTL

    disk = _load_disk_cache()
    if disk:
        _CACHED_SERVERS = disk
        _CACHE_TIME = now
        return disk

    raise RuntimeError(f"Не удалось получить список серверов: {last_err}")

def find_server(query):
    servers = get_servers()
    q_raw = str(query).strip()
    q_lower = q_raw.lower()

    # 1. Если передано число или есть номер сервера ("1", "01", "сервер 1", "#1", "№1", "1 сервер")
    num_match = re.search(r'\d+', q_raw)
    if num_match:
        target_id = int(num_match.group())
        for s in servers:
            if s.get("id") == target_id:
                return s

    # 2. Очищенный поиск по названию (без слова "сервер" и спецсимволов)
    clean_q = re.sub(r'(сервер|server|серв|№|#)', '', q_lower).strip()

    # Точное совпадение по firstname или name
    for s in servers:
        s_first = s.get("firstname", "").lower()
        s_name = s.get("name", "").lower()
        if clean_q and (clean_q == s_first or clean_q == s_name):
            return s

    # Подстрока в firstname или name
    target = clean_q if clean_q else q_lower
    for s in servers:
        s_first = s.get("firstname", "").lower()
        s_name = s.get("name", "").lower()
        if target in s_first or target in s_name:
            return s

    return None
