import json
import re
import time
import urllib.request

API_URLS = [
    "http://api.blackrussia.online/servers.json",
    "https://api.blackrussia.online/servers.json"
]

_CACHED_SERVERS = None
_CACHE_TIME = 0
CACHE_TTL = 300  # 5 минут кэша

def get_servers():
    global _CACHED_SERVERS, _CACHE_TIME
    now = time.time()
    if _CACHED_SERVERS and (now - _CACHE_TIME < CACHE_TTL):
        return _CACHED_SERVERS

    last_err = None
    for url in API_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    _CACHED_SERVERS = data
                    _CACHE_TIME = now
                    return data
        except Exception as e:
            last_err = e

    if _CACHED_SERVERS:
        return _CACHED_SERVERS
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
