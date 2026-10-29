"""Проверка узлов: живой или мёртвый, и годится ли он боту.

Два разных вопроса, и путать их нельзя:

    жив ли узел      TCP-коннект (для TLS-узлов ещё и рукопожатие)
    годится ли боту  сквозной запрос ЧЕРЕЗ прокси до цели

VLESS через REALITY может быть живым, но боту бесполезным: мы этот протокол
не умеем, нужен внешний клиент. Поэтому у результата два флага: `alive` и
`usable`.

Сквозная проверка важнее пинга. Прокси может отвечать на TCP и при этом
молча ронять UDP — а RakNet в SA-MP это целиком UDP. Поэтому для socks5
проверяем ещё и `UDP ASSOCIATE`: без него бот подключится и зависнет в
зоне авторизации.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
from dataclasses import dataclass, asdict

from .vpn_sub import VpnNode

# Куда проверяем сквозной запрос.
#
# ВАЖНО: игровой сервер слушает только UDP 5125, TCP там нет. Поэтому
# проверять прокси коннектом на игровой порт нельзя — получится ложный
# «мёртвый» на живом прокси. Проверяем двумя разными путями:
#
#   TCP  до обычного хоста  — по нему идут pre-ping и HTTP
#   UDP  до STUN-сервера    — по нему идёт RakNet, то есть сам бот
#
# Живой прокси должен пропустить оба. Если UDP не проходит, бот зайдёт и
# зависнет в зоне авторизации — именно это и надо отловить заранее.
DEFAULT_TARGET_HOST = "1.1.1.1"
DEFAULT_TARGET_PORT = 443

# Для HTTP-прокси нужен реальный CONNECT, его проверяем по 443.
HTTP_TARGET_HOST = "api.ipify.org"
HTTP_TARGET_PORT = 443

# STUN: единственный способ узнать, ходит ли UDP через прокси.
STUN_HOST = "stun.l.google.com"
STUN_PORT = 19302
STUN_MAGIC = 0x2112A442

# Проверка ДО ИГРЫ, а не до интернета.
#
# Прокси может прекрасно пускать в интернет и при этом не пускать на
# игровые сервера: Black Russia отвечает не всем адресам, и узел с
# выходом в США или Нидерландах отдаёт пустоту там, где казахстанский
# отдаёт нормальный ответ. Без этой проверки пул считал такие узлы
# рабочими, раздавал их ботам, и те висели в «сервер не отвечает»,
# хотя прокси был жив.
#
# Все игровые сервера лежат в 80.66.82.0/24, поэтому одного адреса
# достаточно как признака «этот выход до игры дотягивается».
GAME_TARGET_HOST = "80.66.82.20"
GAME_TARGET_PORT = 80
GAME_TIMEOUT = 6.0

STATUS_OK = "ok"
# Прокси забанен сервером: с этого адреса сервер не пускает никого.
# Это не «мёртвый» — прокси технически работает, но играть через него
# нельзя никогда, поэтому он не должен попадать в раздачу.
STATUS_BANNED = "banned"
STATUS_DEAD = "dead"
STATUS_NEEDS_CLIENT = "needs_client"
# Прокси живой, UDP идёт, но игровой сервер через него не отвечает.
# Для бота это то же самое, что мёртвый: он не зайдёт.
STATUS_NO_GAME = "no_game"
STATUS_BAD_UDP = "no_udp"
STATUS_UNKNOWN = "unknown"


@dataclass
class ProbeResult:
    node: VpnNode
    alive: bool = False
    usable: bool = False
    status: str = STATUS_UNKNOWN
    latency_ms: float | None = None
    reason: str = ""
    checked_at: float = 0.0
    # SOCKS5-адрес локального моста, если узел поднят через sing-box.
    # Для direct-узлов пусто: там прокси — сам узел.
    socks_url: str = ""

    @property
    def proxy_url(self) -> str:
        """Готовая строка для bot.proxy."""
        return self.socks_url or self.node.proxy_url()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["node"] = self.node.to_dict()
        data["key"] = self.node.key
        data["name"] = self.node.name
        return data

    @property
    def ok(self) -> bool:
        return self.alive and self.usable


def _tcp_latency(host: str, port: int, timeout: float) -> float | None:
    start = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        return (time.monotonic() - start) * 1000.0
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _tls_latency(host: str, port: int, sni: str, timeout: float) -> float | None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    start = time.monotonic()
    raw = None
    wrapped = None
    try:
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        raw.settimeout(timeout)
        wrapped = context.wrap_socket(raw, server_hostname=sni or host)
        return (time.monotonic() - start) * 1000.0
    except (OSError, ssl.SSLError):
        return None
    finally:
        for sock in (wrapped, raw):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _socks5_handshake(sock: socket.socket, login: str, password: str) -> bool:
    methods = b"\x00"
    if login:
        methods = b"\x00\x02"
    sock.sendall(b"\x05" + bytes((len(methods),)) + methods)
    reply = sock.recv(2)
    if len(reply) != 2 or reply[0] != 0x05:
        return False
    if reply[1] == 0x00:
        return True
    if reply[1] != 0x02:
        return False
    user = login.encode("utf-8")[:255]
    pwd = password.encode("utf-8")[:255]
    sock.sendall(b"\x01" + bytes((len(user),)) + user + bytes((len(pwd),)) + pwd)
    auth = sock.recv(2)
    return len(auth) == 2 and auth[1] == 0x00


def _socks5_connect(sock: socket.socket, host: str, port: int) -> bool:
    try:
        packed = socket.inet_aton(host)
        addr = b"\x01" + packed
    except OSError:
        name = host.encode("idna")[:255]
        addr = b"\x03" + bytes((len(name),)) + name
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", int(port)))
    reply = sock.recv(10)
    return len(reply) >= 2 and reply[0] == 0x05 and reply[1] == 0x00


def _socks5_udp_associate(sock: socket.socket) -> tuple[str, int] | None:
    sock.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
    reply = sock.recv(10)
    if len(reply) < 10 or reply[1] != 0x00:
        return None
    if reply[3] == 0x01:
        host = ".".join(str(b) for b in reply[4:8])
    elif reply[3] == 0x04:
        host = socket.inet_ntop(socket.AF_INET6, reply[4:20])
    else:
        return None
    port = struct.unpack(">H", reply[8:10])[0]
    return host, port


def _stun_probe_udp(
    udp: socket.socket,
    relay: tuple[str, int],
    timeout: float,
    *,
    attempts: int = 3,
) -> tuple[str | None, float | None]:
    """STUN binding через SOCKS5 UDP.

    Возвращает (внешний ip:port, время круга в мс).

    UDP теряет пакеты — это его нормальное поведение, а не признак смерти
    узла. Одна попытка давала ложные «UDP не проходит» на живых узлах:
    при повторных запусках тот же узел с тем же таймаутом то проходил, то
    нет. Поэтому несколько попыток, и только если все пусты — отказ.

    Время круга берётся по STUN: это настоящая дорога до узла и обратно,
    в отличие от ответа на CONNECT, который локальный мост отдаёт сразу,
    не дожидаясь удалённой стороны.
    """
    import os

    try:
        target_ip = socket.gethostbyname(STUN_HOST)
    except OSError:
        return None, None

    external = None
    samples: list[float] = []

    for _ in range(max(1, int(attempts))):
        transaction = os.urandom(12)
        request = struct.pack(">HHI", 0x0001, 0, STUN_MAGIC) + transaction
        header = b"\x00\x00\x00\x01" + socket.inet_aton(target_ip) + struct.pack(">H", STUN_PORT)
        started = time.monotonic()
        try:
            udp.sendto(header + request, relay)
            data, _ = udp.recvfrom(2048)
        except OSError:
            continue
        rtt = (time.monotonic() - started) * 1000.0
        if len(data) < 12:
            continue
        found = _parse_stun_external(data)
        if not found:
            continue
        external = found
        samples.append(rtt)
        # Туннель прогрет и отвечает быстро — дальше мерить нечего.
        if len(samples) >= 2 and min(samples) < 300.0:
            break

    if external is None or not samples:
        return None, None

    # Минимум, а не последнее значение: UDP теряет и переспрашивает, и
    # единственный замер почти всегда завышен. Минимум из нескольких — это
    # и есть дорога до узла без очередей и повторов.
    return external, min(samples)


def _parse_stun_external(data: bytes) -> str | None:
    """Достать внешний ip:port из ответа STUN, срезав заголовок SOCKS5."""
    if len(data) < 12:
        return None
    atyp = data[3]
    if atyp == 0x01:
        body = data[10:]
    elif atyp == 0x04:
        body = data[22:]
    elif atyp == 0x03:
        body = data[5 + data[4]:]
    else:
        return None
    if len(body) < 20:
        return None
    length = struct.unpack(">H", body[2:4])[0]
    offset, finish = 20, 20 + length
    while offset + 4 <= finish:
        attr_type, attr_len = struct.unpack(">HH", body[offset:offset + 4])
        value = body[offset + 4:offset + 4 + attr_len]
        if attr_type == 0x0020 and len(value) >= 8:
            port = struct.unpack(">H", value[2:4])[0] ^ 0x2112
            raw_ip = bytes(b ^ k for b, k in zip(value[4:8], b"\x21\x12\xa4\x42"))
            return f"{socket.inet_ntoa(raw_ip)}:{port}"
        offset += 4 + attr_len + ((4 - attr_len % 4) % 4)
    return None


def verify_socks5(
    host: str,
    port: int,
    *,
    login: str = "",
    password: str = "",
    target_host: str = DEFAULT_TARGET_HOST,
    target_port: int = DEFAULT_TARGET_PORT,
    timeout: float = 8.0,
    check_udp: bool = True,
    game_host: str = "",
    game_port: int = GAME_TARGET_PORT,
) -> tuple[bool, bool, str, float | None]:
    """Сквозная проверка SOCKS5: TCP до цели и UDP через STUN.

    TCP и UDP проверяются на РАЗНЫХ управляющих соединениях. После CONNECT
    соединение уходит в туннель, и UDP ASSOCIATE на нём сервер уже не
    обрабатывает — проверка висела бы до таймаута на живом прокси.

    Возвращает (живой, годится_боту, причина, задержка_мс).
    """
    start = time.monotonic()

    # --- TCP: реальный CONNECT до внешнего хоста ---
    ctrl = None
    try:
        ctrl = socket.create_connection((host, int(port)), timeout=timeout)
        ctrl.settimeout(timeout)
        if not _socks5_handshake(ctrl, login, password):
            return False, False, "socks5: отказ авторизации", None
        if not _socks5_connect(ctrl, target_host, target_port):
            return True, False, "socks5: CONNECT до цели не прошёл", None
        latency = (time.monotonic() - start) * 1000.0
    except OSError as exc:
        return False, False, f"socks5: {type(exc).__name__}", None
    finally:
        if ctrl is not None:
            try:
                ctrl.close()
            except OSError:
                pass

    if not check_udp:
        return True, True, "", latency

    # --- UDP: отдельное соединение, только ASSOCIATE + STUN ---
    udp_ctrl = None
    udp = None
    try:
        udp_ctrl = socket.create_connection((host, int(port)), timeout=timeout)
        udp_ctrl.settimeout(timeout)
        if not _socks5_handshake(udp_ctrl, login, password):
            return True, False, "socks5: UDP — отказ авторизации", latency
        relay = _socks5_udp_associate(udp_ctrl)
        if relay is None:
            return True, False, "socks5: UDP ASSOCIATE не поддерживается", latency
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(timeout)
        external, rtt = _stun_probe_udp(udp, relay, timeout)
    except OSError as exc:
        return True, False, f"socks5: UDP — {type(exc).__name__}", latency
    finally:
        for item in (udp, udp_ctrl):
            if item is not None:
                try:
                    item.close()
                except OSError:
                    pass

    if not external:
        return True, False, "socks5: UDP через прокси не проходит", latency

    # Пинг берём по времени круга STUN, а не по ответу на CONNECT.
    # Локальный мост (sing-box) отвечает на CONNECT сразу, ещё не сходив к
    # узлу, поэтому его время — это коннект до 127.0.0.1, то есть около
    # 1 мс. Настоящая дорога видна только по реальному ответу из сети.
    if rtt:
        latency = rtt

    # Прокси пускает в интернет — но пустит ли он в игру? Это отдельный
    # вопрос, и без него бот входит в «сервер не отвечает».
    if game_host:
        reachable, why = _game_reachable(
            host, port, login=login, password=password,
            game_host=game_host, game_port=game_port, timeout=timeout,
        )
        if not reachable:
            return True, False, why, latency

    return True, True, f"выход {external}", latency


def _game_reachable(host, port, *, login="", password="", game_host="",
                    game_port=GAME_TARGET_PORT, timeout=GAME_TIMEOUT):
    """Отвечает ли игровой сервер через этот прокси.

    Делаем то же, что делает бот перед входом: CONNECT на игровой порт и
    короткий HTTP-запрос. Пустой ответ — сервер нас не видит, и бот
    дальше не пройдёт. Возвращает (дошло, причина).
    """
    if not game_host:
        return True, ""
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(timeout)
        if not _socks5_handshake(sock, login, password):
            return False, "игра: отказ авторизации"
        if not _socks5_connect(sock, game_host, int(game_port)):
            return False, "игра: CONNECT не прошёл"
        sock.sendall(
            f"GET / HTTP/1.1\r\nHost: {game_host}\r\n"
            "Connection: close\r\nUser-Agent: Dalvik/2.1.0\r\n\r\n".encode()
        )
        data = sock.recv(64)
        if not data:
            return False, "игра не отвечает через этот узел"
        return True, ""
    except OSError as exc:
        return False, f"игра: {type(exc).__name__}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def verify_http(
    host: str,
    port: int,
    *,
    login: str = "",
    password: str = "",
    target_host: str = HTTP_TARGET_HOST,
    target_port: int = HTTP_TARGET_PORT,
    timeout: float = 8.0,
    use_tls: bool = False,
) -> tuple[bool, bool, str, float | None]:
    """Сквозная проверка HTTP-прокси через CONNECT."""
    import base64 as _b64

    start = time.monotonic()
    raw = None
    sock = None
    try:
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        raw.settimeout(timeout)
        if use_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        lines = [
            f"CONNECT {target_host}:{int(target_port)} HTTP/1.1",
            f"Host: {target_host}:{int(target_port)}",
            "Proxy-Connection: Keep-Alive",
        ]
        if login:
            token = _b64.b64encode(f"{login}:{password}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        reply = sock.recv(64)
        latency = (time.monotonic() - start) * 1000.0
        if not reply.upper().startswith(b"HTTP/"):
            return False, False, "http: прокси не ответил по HTTP", None
        if b" 200" not in reply.split(b"\r\n", 1)[0]:
            code = reply.split(b" ", 2)[1:2]
            return True, False, f"http: CONNECT отказ {code}", latency
        # HTTP-прокси не умеет UDP, а RakNet без UDP не работает.
        return True, False, "http: прокси не держит UDP (нужен socks5)", latency
    except OSError as exc:
        return False, False, f"http: {type(exc).__name__}", None
    finally:
        for item in (sock, raw):
            if item is not None:
                try:
                    item.close()
                except OSError:
                    pass


def probe_node(
    node: VpnNode,
    *,
    timeout: float = 6.0,
    target_host: str = DEFAULT_TARGET_HOST,
    target_port: int = DEFAULT_TARGET_PORT,
    check_udp: bool = True,
    game_host: str = "",
    game_port: int = GAME_TARGET_PORT,
) -> ProbeResult:
    """Проверить один узел."""
    result = ProbeResult(node=node, checked_at=time.time())

    if node.stub:
        result.status = STATUS_DEAD
        result.reason = "заглушка панели"
        return result

    if not node.address or not node.port:
        result.status = STATUS_DEAD
        result.reason = "нет адреса или порта"
        return result

    if node.protocol in ("socks5", "socks4", "socks4a"):
        alive, usable, reason, latency = verify_socks5(
            node.address,
            node.port,
            login=node.login,
            password=node.password,
            target_host=target_host,
            target_port=target_port,
            timeout=timeout,
            check_udp=check_udp,
            game_host=game_host,
            game_port=game_port,
        )
        result.alive = alive
        result.usable = usable
        result.latency_ms = latency
        result.reason = reason
        if usable:
            result.status = STATUS_OK
        elif reason.startswith("игра"):
            result.status = STATUS_NO_GAME
        elif alive:
            result.status = STATUS_BAD_UDP
        else:
            result.status = STATUS_DEAD
        return result

    if node.protocol in ("http", "https"):
        alive, usable, reason, latency = verify_http(
            node.address,
            node.port,
            login=node.login,
            password=node.password,
            timeout=timeout,
            use_tls=(node.protocol == "https"),
        )
        result.alive = alive
        result.usable = usable
        result.latency_ms = latency
        result.reason = reason
        result.status = STATUS_OK if usable else (STATUS_BAD_UDP if alive else STATUS_DEAD)
        return result

    # Узлы, которым нужен внешний клиент. Проверяем только живость:
    # TCP, а для TLS/REALITY ещё и рукопожатие по нужному SNI.
    if node.uses_tls:
        latency = _tls_latency(node.address, node.port, node.sni, timeout)
        if latency is None:
            # TLS-узел без TLS — мёртвый, а не «живой наполовину». Быстрый
            # TCP-коннект при этом ничего не значит: VPN-клиент на телефоне
            # отвечает на SYN локально, и до реального сервера дело не
            # доходит. Ровно так выглядели kaz/usa/rus — 1мс и тишина.
            result.status = STATUS_DEAD
            result.reason = "TLS не поднялся (узел не отвечает)"
            return result
    else:
        latency = _tcp_latency(node.address, node.port, timeout)
        if latency is None:
            result.status = STATUS_DEAD
            result.reason = "нет TCP-коннекта"
            return result

    result.alive = True
    result.usable = False
    result.latency_ms = latency
    result.status = STATUS_NEEDS_CLIENT
    result.reason = f"{node.protocol.upper()} — нужен внешний клиент"
    return result


def probe_many(
    nodes: list[VpnNode],
    *,
    timeout: float = 6.0,
    target_host: str = DEFAULT_TARGET_HOST,
    target_port: int = DEFAULT_TARGET_PORT,
    workers: int = 8,
    game_host: str = "",
    game_port: int = GAME_TARGET_PORT,
) -> list[ProbeResult]:
    """Проверить пачку узлов параллельно."""
    if not nodes:
        return []
    from concurrent.futures import ThreadPoolExecutor

    results: list[ProbeResult] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(nodes)))) as pool:
        futures = [
            pool.submit(
                probe_node,
                node,
                timeout=timeout,
                target_host=target_host,
                target_port=target_port,
                game_host=game_host,
                game_port=game_port,
            )
            for node in nodes
        ]
        for future in futures:
            try:
                results.append(future.result())
            except Exception as exc:  # поток не должен ронять пул
                results.append(
                    ProbeResult(
                        node=nodes[0],
                        status=STATUS_DEAD,
                        reason=f"сбой проверки: {exc}",
                        checked_at=time.time(),
                    )
                )
    return results
