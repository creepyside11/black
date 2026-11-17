"""Isolated, bounded SOCKS5 batch checks; does not modify the bot proxy pool."""
from __future__ import annotations
import concurrent.futures
import ipaddress
import os
import socket
import struct
import threading
from .vpn_sub import parse_link
from .vpn_probe import _tcp_carries, _socks5_handshake, STUN_HOST, STUN_PORT, STUN_MAGIC

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_PROXIES = 20000
CHECK_LOCK = threading.Lock()


def parse_proxies(content):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_INPUT_BYTES:
        raise ValueError('Файл должен быть UTF-8 и не больше 2 МиБ.')
    nodes, seen = [], set()
    for row in content.splitlines():
        row = row.strip()
        if not row or row.startswith('#'):
            continue
        if len(row) > 500:
            continue
        candidate = row if '://' in row else 'socks5://' + row
        node = parse_link(candidate)
        if node is None or node.protocol != 'socks5' or not 1 <= node.port <= 65535:
            continue
        # Only public addresses; prevent a pasted list from scanning the VPS,
        # LAN, metadata endpoints or creating DNS-rebinding requests.
        try:
            ip = ipaddress.ip_address(node.address)
        except ValueError:
            continue
        if ip.version != 4 or not ip.is_global:
            continue
        identity = (node.address, node.port, node.login, node.password)
        if identity in seen:
            continue
        seen.add(identity)
        nodes.append(node)
        if len(nodes) > MAX_PROXIES:
            raise ValueError('Не больше 20000 прокси за одну проверку.')
    if not nodes:
        raise ValueError('Не найдено SOCKS5 с публичным IPv4 и портом.')
    return nodes


def worker_count():
    # Bounded concurrency protects small VPSs; an owner may increase it.
    cpus = os.cpu_count() or 1
    try:
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass
    try:
        with open('/sys/fs/cgroup/cpu.max', encoding='ascii') as f:
            quota, period = f.read().split()[:2]
        if quota != 'max':
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    default = min(64, max(8, cpus * 16))
    try:
        value = int(os.environ.get('BR_PROXY_CHECK_WORKERS', default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(128, value))


def check_one(node, timeout=4.0):
    try:
        tcp, _ = _tcp_carries(node.address, node.port, login=node.login,
                              password=node.password, timeout=timeout)
        if not tcp:
            return node, False, False
        udp = check_udp(node, timeout)
        return node, True, bool(udp)
    except (OSError, ValueError, OverflowError):
        return node, False, False


def _read_exact(sock, count):
    result = b''
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise OSError('short reply')
        result += data
    return result


def check_udp(node, timeout):
    """Verify UDP relay with a matching STUN transaction from a public relay."""
    with socket.create_connection((node.address, node.port), timeout=timeout) as ctrl:
        ctrl.settimeout(timeout)
        if not _socks5_handshake(ctrl, node.login, node.password):
            return False
        ctrl.sendall(b'\x05\x03\x00\x01' + b'\x00' * 6)
        header = _read_exact(ctrl, 4)
        if header[:3] != b'\x05\x00\x00' or header[3] != 1:
            return False
        host = socket.inet_ntoa(_read_exact(ctrl, 4))
        port = struct.unpack('>H', _read_exact(ctrl, 2))[0]
        # Only the proxy itself may receive packets, not an arbitrary LAN host.
        if host == '0.0.0.0': host = node.address
        if host != node.address or not port:
            return False
        target = socket.gethostbyname(STUN_HOST)
        envelope = b'\x00\x00\x00\x01' + socket.inet_aton(target) + struct.pack('>H', STUN_PORT)
        transaction = os.urandom(12)
        request = struct.pack('>HHI', 1, 0, STUN_MAGIC) + transaction
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(timeout)
            udp.connect((host, port))
            udp.send(envelope + request)
            data = udp.recv(2048)
        if len(data) < 30 or data[:4] != b'\x00\x00\x00\x01':
            return False
        source = socket.inet_ntoa(data[4:8]); source_port = struct.unpack('>H', data[8:10])[0]
        body = data[10:]
        return (source == target and source_port == STUN_PORT and len(body) >= 20
                and body[:2] == b'\x01\x01' and body[4:8] == struct.pack('>I', STUN_MAGIC)
                and body[8:20] == transaction)


def check_many(nodes, *, workers=None, timeout=4.0):
    limit = min(128, max(1, int(workers if workers is not None else worker_count())))
    # executor.map keeps result order; no access to manager, sessions, bridge or pool.
    with concurrent.futures.ThreadPoolExecutor(max_workers=limit, thread_name_prefix='proxy-check') as executor:
        return list(executor.map(lambda node: check_one(node, timeout), nodes))
