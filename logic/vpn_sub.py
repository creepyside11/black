"""Разбор VPN-подписок: ссылка -> список серверов.

Подписка отдаёт одно из трёх:

1. base64 от строк `vless://...` через перевод строки (самый частый вид)
2. те же строки без base64, обычным текстом
3. JSON-массив готовых конфигов xray (Remnawave и подобные панели отдают
   именно это, если попросить заголовком `x-hwid`)

Панель может не отдавать узлы вообще, а подсунуть заглушку — vless с нулевым
UUID на `0.0.0.0:1` и текстом «приложение не поддерживается». Такие узлы
надо распознавать и выбрасывать, иначе они попадут в список как живые.

Узлы делятся на две группы:

    direct        socks5:// и http:// — с ними бот работает сам, без клиента
    needs_client  vless, vmess, trojan, ss — нужен внешний клиент, мы такой
                  протокол не умеем

Вторая группа всё равно полезна: её видно в списке, её пингуют, и по ней
понятно, живёт подписка или нет.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import socket
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict

# Протоколы, которые бот умеет сам, через samp/proxy.py.
DIRECT_PROTOCOLS = ("socks5", "socks5h", "socks4", "socks4a", "http", "https")

# Всё остальное — только через внешний клиент.
CLIENT_PROTOCOLS = ("vless", "vmess", "trojan", "ss", "ssr", "hysteria", "hysteria2", "tuic")

ALL_PROTOCOLS = DIRECT_PROTOCOLS + CLIENT_PROTOCOLS

# Заглушки панелей: такой узел не несёт ничего.
# 127.0.0.1 здесь быть не должно: локальный прокси — обычное дело.
STUB_HOSTS = ("0.0.0.0", "::", "example.com")
STUB_PORTS = (0, 1)
STUB_MARKERS = (
    "не поддерживается",
    "not supported",
    "hwid is not enabled",
    "не включён hwid",
    "не включен hwid",
)


class SubscriptionError(Exception):
    """Подписку не удалось получить или разобрать."""


@dataclass
class VpnNode:
    """Один сервер из подписки."""

    name: str
    protocol: str
    address: str
    port: int
    security: str = "none"
    sni: str = ""
    login: str = ""
    password: str = ""
    raw: str = ""
    source: str = ""
    stub: bool = False

    @property
    def is_direct(self) -> bool:
        return self.protocol in DIRECT_PROTOCOLS

    @property
    def needs_client(self) -> bool:
        return self.protocol in CLIENT_PROTOCOLS

    @property
    def key(self) -> str:
        """Уникальный идентификатор узла.

        Одного адреса и порта мало: у подписки на одном хосте висит
        несколько узлов с разными UUID, и они сливались в один — общий
        порт моста, общий туннель, а боту отдавался чужой. Логин и есть
        то, что отличает аккаунты на одном сервере.
        """
        base = f"{self.protocol}://{self.address}:{self.port}"
        if self.login:
            base = f"{base}#{self.login}"
        # Логин тоже не всегда различает: подписка выдаёт всем узлам один
        # и тот же UUID, а отличается SNI и ключ reality. Отпечаток
        # конфига разводит их гарантированно.
        #
        # Только для узлов, которым нужен мост: у socks5/http адрес и порт
        # уже уникальны, а отпечаток сделал бы ключ нечитаемым — по нему
        # ищут узел и сверяют статусы.
        if self.raw and not self.is_direct:
            digest = hashlib.sha256(self.raw.encode("utf-8")).hexdigest()[:10]
            base = f"{base}@{digest}"
        return base

    @property
    def uses_tls(self) -> bool:
        return self.security in ("tls", "reality", "xtls")

    def proxy_url(self) -> str:
        """Строка для bot.proxy. Только для direct-узлов."""
        if not self.is_direct:
            return ""
        auth = ""
        if self.login:
            login = urllib.parse.quote(self.login, safe="")
            password = urllib.parse.quote(self.password, safe="")
            auth = f"{login}:{password}@"
        return f"{self.protocol}://{auth}{self.address}:{self.port}"

    def to_dict(self) -> dict:
        return asdict(self)


def _b64decode_any(text: str) -> str | None:
    """base64 с любым паддингом и url-safe алфавитом."""
    raw = text.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if not raw:
        return None
    raw = raw.replace("-", "+").replace("_", "/")
    pad = (-len(raw)) % 4
    try:
        return base64.b64decode(raw + "=" * pad).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def _looks_like_links(text: str) -> bool:
    lowered = text.lower()
    return any(f"{proto}://" in lowered for proto in ALL_PROTOCOLS)


def _is_stub(host: str, port: int, name: str) -> bool:
    if (host or "").lower() in STUB_HOSTS:
        return True
    if int(port or 0) in STUB_PORTS:
        return True
    lowered = (name or "").lower()
    return any(marker in lowered for marker in STUB_MARKERS)


def _parse_vless(link: str, source: str) -> VpnNode | None:
    # vless://uuid@host:port?params#name
    body = link[len("vless://"):]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    if "@" not in body:
        return None
    _uuid, hostpart = body.rsplit("@", 1)
    host, port = _split_hostport(hostpart)
    if not host:
        return None
    params = urllib.parse.parse_qs(query)
    security = (params.get("security", ["none"])[0] or "none").lower()
    sni = params.get("sni", [""])[0] or params.get("host", [""])[0] or host
    return VpnNode(
        name=name or host,
        protocol="vless",
        address=host,
        port=port,
        security=security,
        sni=sni,
        raw=link,
        source=source,
        stub=_is_stub(host, port, name),
    )


def _parse_vmess(link: str, source: str) -> VpnNode | None:
    # vmess://base64(json)
    payload = _b64decode_any(link[len("vmess://"):])
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    host = str(data.get("add") or "")
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    name = str(data.get("ps") or host)
    if not host:
        return None
    return VpnNode(
        name=name,
        protocol="vmess",
        address=host,
        port=port,
        security=(str(data.get("tls") or "none")).lower(),
        sni=str(data.get("sni") or data.get("host") or host),
        raw=link,
        source=source,
        stub=_is_stub(host, port, name),
    )


def _parse_trojan(link: str, source: str) -> VpnNode | None:
    body = link[len("trojan://"):]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    if "@" not in body:
        return None
    password, hostpart = body.rsplit("@", 1)
    host, port = _split_hostport(hostpart)
    if not host:
        return None
    params = urllib.parse.parse_qs(query)
    sni = params.get("sni", [""])[0] or params.get("peer", [""])[0] or host
    return VpnNode(
        name=name or host,
        protocol="trojan",
        address=host,
        port=port,
        security="tls",
        sni=sni,
        password=urllib.parse.unquote(password),
        raw=link,
        source=source,
        stub=_is_stub(host, port, name),
    )


def _parse_ss(link: str, source: str) -> VpnNode | None:
    # ss://base64(method:password)@host:port#name
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    if "@" not in body:
        return None
    creds, hostpart = body.rsplit("@", 1)
    decoded = _b64decode_any(creds)
    if decoded and ":" in decoded:
        login, password = decoded.split(":", 1)
    else:
        login, password = "", ""
    host, port = _split_hostport(hostpart.split("?")[0])
    if not host:
        return None
    return VpnNode(
        name=name or host,
        protocol="ss",
        address=host,
        port=port,
        login=login,
        password=password,
        raw=link,
        source=source,
        stub=_is_stub(host, port, name),
    )


def _parse_direct(link: str, source: str) -> VpnNode | None:
    scheme = link.split("://", 1)[0].lower()
    rest = link.split("://", 1)[1]
    name = ""
    if "#" in rest:
        rest, name = rest.split("#", 1)
        name = urllib.parse.unquote(name)
    rest = rest.split("?", 1)[0]
    login = password = ""
    if "@" in rest:
        creds, rest = rest.rsplit("@", 1)
        if ":" in creds:
            login, password = creds.split(":", 1)
        else:
            login = creds
    host, port = _split_hostport(rest)
    if not host or not port:
        return None
    return VpnNode(
        name=name or f"{host}:{port}",
        protocol="socks5" if scheme == "socks5h" else scheme,
        address=host,
        port=port,
        login=urllib.parse.unquote(login),
        password=urllib.parse.unquote(password),
        raw=link,
        source=source,
        stub=_is_stub(host, port, name),
    )


def _split_hostport(value: str) -> tuple[str, int]:
    value = (value or "").strip().rstrip("/")
    if not value:
        return "", 0
    # [ipv6]:port
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return "", 0
        host = value[1:end]
        tail = value[end + 1:].lstrip(":")
        try:
            return host, int(tail or 0)
        except ValueError:
            return host, 0
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        try:
            return host.strip(), int(port)
        except ValueError:
            return host.strip(), 0
    return value, 0


_PARSERS = {
    "vless": _parse_vless,
    "vmess": _parse_vmess,
    "trojan": _parse_trojan,
    "ss": _parse_ss,
}


def parse_link(link: str, source: str = "") -> VpnNode | None:
    """Одна ссылка -> узел. None, если формат незнакомый."""
    link = (link or "").strip()
    if "://" not in link:
        return None
    scheme = link.split("://", 1)[0].lower()
    if scheme in _PARSERS:
        try:
            return _PARSERS[scheme](link, source)
        except Exception:
            return None
    if scheme in DIRECT_PROTOCOLS:
        try:
            return _parse_direct(link, source)
        except Exception:
            return None
    return None


def _outbound_raw(out: dict) -> str:
    """Узел в виде конфига из одного outbound.

    Раньше здесь лежал весь конфиг Xray, обрезанный до 2000 символов.
    Обрезка рвала JSON посередине строки — мост получал невалидный
    документ и молча не поднимался, а узел висел «живым, но неприменимым».
    Нужен мосту только сам outbound, он в разы меньше и влезает целиком.
    """
    return json.dumps({"outbounds": [out]}, ensure_ascii=False)


def _parse_xray_config(config: dict, source: str) -> list[VpnNode]:
    """Один конфиг xray -> узлы из его outbounds."""
    nodes = []
    name = str(config.get("remarks") or config.get("tag") or "")
    for out in config.get("outbounds") or []:
        if not isinstance(out, dict):
            continue
        proto = str(out.get("protocol") or "").lower()
        if proto not in ("vless", "vmess", "trojan", "shadowsocks"):
            continue
        if proto == "shadowsocks":
            proto = "ss"
        settings = out.get("settings") or {}
        stream = out.get("streamSettings") or {}
        security = str(stream.get("security") or "none").lower()
        reality = stream.get("realitySettings") or {}
        tls = stream.get("tlsSettings") or {}
        sni = str(
            reality.get("serverName")
            or tls.get("serverName")
            or ""
        )
        for server in settings.get("vnext") or []:
            users = server.get("users") or [{}]
            user = users[0] if users else {}
            host = str(server.get("address") or "")
            try:
                port = int(server.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if not host:
                continue
            nodes.append(
                VpnNode(
                    name=name or host,
                    protocol=proto,
                    address=host,
                    port=port,
                    security=security,
                    sni=sni or host,
                    login=str(user.get("id") or ""),
                    raw=_outbound_raw(out),
                    source=source,
                    stub=_is_stub(host, port, name),
                )
            )
        for server in settings.get("servers") or []:
            host = str(server.get("address") or "")
            try:
                port = int(server.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if not host:
                continue
            nodes.append(
                VpnNode(
                    name=name or host,
                    protocol=proto,
                    address=host,
                    port=port,
                    security=security,
                    sni=sni or host,
                    password=str(server.get("password") or ""),
                    raw=_outbound_raw(out),
                    source=source,
                    stub=_is_stub(host, port, name),
                )
            )
    return nodes


def decode_subscription(text: str, source: str = "") -> list[VpnNode]:
    """Тело подписки в любом известном виде -> узлы.

    Порядок проб важен: сначала JSON, потом base64, потом plain. Иначе
    base64-декодер может «успешно» разобрать JSON в мусор.
    """
    if not text or not text.strip():
        raise SubscriptionError("Пустой ответ подписки")

    body = text.strip()
    nodes: list[VpnNode] = []

    # 1. JSON: массив конфигов xray либо один конфиг
    if body.startswith("[") or body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if data is not None:
            configs = data if isinstance(data, list) else [data]
            for config in configs:
                if isinstance(config, dict):
                    nodes.extend(_parse_xray_config(config, source))
            if nodes:
                return _reject_all_stubs(nodes, body)

    # 2. Ссылки как есть
    if _looks_like_links(body):
        return _reject_all_stubs(_parse_lines(body, source), body)

    # 3. base64
    decoded = _b64decode_any(body)
    if decoded and _looks_like_links(decoded):
        return _reject_all_stubs(_parse_lines(decoded, source), body)

    raise SubscriptionError(
        "Подписка не содержит узлов: ни JSON-конфигов, ни ссылок "
        f"(начало ответа: {body[:120]!r})"
    )


def _parse_lines(text: str, source: str) -> list[VpnNode]:
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        node = parse_link(line, source)
        if node is not None:
            nodes.append(node)
    return nodes


def _reject_all_stubs(nodes: list[VpnNode], body: str) -> list[VpnNode]:
    """Все узлы — заглушки? Значит панель не пустила, и это надо сказать вслух."""
    if not nodes or any(not node.stub for node in nodes):
        return nodes
    marker = next(
        (node.name for node in nodes if any(m in node.name.lower() for m in STUB_MARKERS)),
        "",
    )
    hint = f" Панель говорит: {marker!r}." if marker else ""
    raise SubscriptionError(
        "Подписка отдала только заглушки вместо серверов."
        f"{hint} Обычно это значит, что в панели не включён HWID — "
        "без него список узлов не выдаётся."
    )


def fetch_subscription(
    url: str,
    *,
    timeout: float = 15.0,
    hwid: str = "",
    user_agent: str = "Happ/2.16.1",
) -> str:
    """Скачать тело подписки.

    Панели Remnawave не отдают узлы без `x-hwid` — вместо списка приходит
    заглушка «HWID is not enabled». С заголовком приходит JSON с конфигами.
    Поэтому пробуем сначала с HWID, и только если узлов не нашлось — без.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise SubscriptionError(f"Некорректная ссылка подписки: {url!r}")

    def _get(extra_headers: dict) -> str:
        request = urllib.request.Request(url)
        request.add_header("User-Agent", user_agent)
        request.add_header("Accept", "*/*")
        for key, value in extra_headers.items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    headers = {}
    if hwid:
        headers = {
            "x-hwid": hwid,
            "x-device-os": "Android",
            "x-ver-os": "12",
            "x-device-model": "CPH2597",
        }

    errors = []
    for extra in (headers, {}) if headers else ({},):
        try:
            body = _get(extra)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            continue
        try:
            nodes = decode_subscription(body, url)
        except SubscriptionError as exc:
            errors.append(str(exc))
            continue
        if any(not node.stub for node in nodes):
            return body

    raise SubscriptionError(
        "Подписка не отдала рабочих узлов. " + ("; ".join(errors[-2:]) or "")
    )


def load_subscription(url: str, **kwargs) -> list[VpnNode]:
    """Скачать и разобрать. Заглушки отбрасываются."""
    body = fetch_subscription(url, **kwargs)
    nodes = decode_subscription(body, url)
    return [node for node in nodes if not node.stub]


def dedupe(nodes: list[VpnNode]) -> list[VpnNode]:
    """Убрать повторы по protocol://host:port, сохранив порядок."""
    seen = set()
    result = []
    for node in nodes:
        key = node.key
        if key in seen:
            continue
        seen.add(key)
        result.append(node)
    return result


def probe_tcp(address: str, port: int, timeout: float = 5.0) -> float | None:
    """TCP-коннект. Возвращает мс или None. Для пинга узлов."""
    if not address or not port:
        return None
    import time

    start = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((address, int(port)), timeout=timeout)
        return (time.monotonic() - start) * 1000.0
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
