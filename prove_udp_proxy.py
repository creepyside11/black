#!/usr/bin/env python3
"""Прямое доказательство: что реально везёт узел — TCP, UDP или ничего.

Почему не «socks5 CONNECT сказал ok»: sing-box отвечает успехом на CONNECT
до того, как открыл соединение к узлу. Поэтому TCP проверяется данными —
HTTP-запрос до игрового хоста и ожидание непустого ответа. UDP проверяется
STUN binding через SOCKS5 UDP ASSOCIATE. Иначе оба вердикта — догадка.

  python3 prove_udp_proxy.py            # все узлы подписки + локальный HAPP
  python3 prove_udp_proxy.py <link>     # один узел из ссылки
"""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic import vpn_bridge as B
from logic import vpn_sub as S

STUN_HOST = "stun.l.google.com"
STUN_PORT = 19302
STUN_MAGIC = 0x2112A442
GAME_HOST = "80.66.82.20"
GAME_PORT = 80
TIMEOUT = 8.0


def log(line: str) -> None:
    print(line, flush=True)


def handshake(sock: socket.socket) -> bool:
    sock.sendall(b"\x05\x01\x00")
    r = sock.recv(2)
    return len(r) == 2 and r[0] == 0x05 and r[1] == 0x00


def _addr(host: str, port: int) -> bytes:
    try:
        return b"\x01" + socket.inet_aton(host) + struct.pack(">H", port)
    except OSError:
        name = host.encode("idna")[:255]
        return b"\x03" + bytes((len(name),)) + name + struct.pack(">H", port)


def tcp_real(host: str, port: int, *, target: str, tport: int, timeout: float) -> str:
    """Реальный TCP: CONNECT + данные. Возвращает вердикт строкой."""
    s = None
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.settimeout(timeout)
        if not handshake(s):
            return "SOCKS5 handshake отказ"
        s.sendall(b"\x05\x01\x00" + _addr(target, tport))
        r = s.recv(10)
        if len(r) < 2 or r[1] != 0x00:
            return f"CONNECT отказ ({r.hex() or 'пусто'})"
        s.sendall(f"GET / HTTP/1.1\r\nHost: {target}\r\nConnection: close\r\n"
                  "User-Agent: Dalvik/2.1.0\r\n\r\n".encode())
        data = s.recv(256)
        if not data:
            return "CONNECT ок, но данных нет — туннель не везёт"
        return f"везёт TCP ({data[:24]!r})"
    except OSError as exc:
        return f"TCP не прошёл: {type(exc).__name__}"
    finally:
        if s is not None:
            s.close()


def udp_real(host: str, port: int, *, timeout: float) -> str:
    ctrl = udp = None
    try:
        ctrl = socket.create_connection((host, int(port)), timeout=timeout)
        ctrl.settimeout(timeout)
        if not handshake(ctrl):
            return "SOCKS5 handshake отказ"
        ctrl.sendall(b"\x05\x03\x00\x01" + b"\x00" * 4 + b"\x00\x00")
        r = ctrl.recv(10)
        if len(r) < 10 or r[1] != 0x00:
            return f"UDP ASSOCIATE отказ ({r.hex() or 'пусто'})"
        relay = (".".join(str(b) for b in r[4:8]), struct.unpack(">H", r[8:10])[0])
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(timeout)
        try:
            target_ip = socket.gethostbyname(STUN_HOST)
        except OSError as exc:
            return f"STUN DNS не разрешился: {exc}"
        for _ in range(3):
            req = struct.pack(">HHI", 1, 0, STUN_MAGIC) + os.urandom(12)
            pkt = (b"\x00\x00\x00\x01" + socket.inet_aton(target_ip)
                   + struct.pack(">H", STUN_PORT) + req)
            try:
                udp.sendto(pkt, relay)
                data, _ = udp.recvfrom(2048)
            except OSError:
                continue
            ext = _stun_external(data)
            if ext:
                return f"везёт UDP (внешний {ext}, relay {relay[0]}:{relay[1]})"
            return f"ответ не STUN ({data[:16].hex()})"
        return f"UDP ASSOCIATE ок (relay {relay[0]}:{relay[1]}), ответа нет"
    except OSError as exc:
        return f"UDP не прошёл: {type(exc).__name__}"
    finally:
        for item in (udp, ctrl):
            if item is not None:
                item.close()


def _stun_external(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    atyp = data[3]
    body = data[10:] if atyp == 0x01 else (data[22:] if atyp == 0x04 else None)
    if body is None or len(body) < 20:
        return None
    length = struct.unpack(">H", body[2:4])[0]
    off, fin = 20, 20 + length
    while off + 4 <= fin:
        atype, alen = struct.unpack(">HH", body[off:off + 4])
        val = body[off + 4:off + 4 + alen]
        if atype == 0x0020 and len(val) >= 8:
            prt = struct.unpack(">H", val[2:4])[0] ^ 0x2112
            raw = bytes(b ^ k for b, k in zip(val[4:8], b"\x21\x12\xa4\x42"))
            return f"{socket.inet_ntoa(raw)}:{prt}"
        off += 4 + alen + ((4 - alen % 4) % 4)
    return None


def fetch_nodes() -> list[S.VpnNode]:
    pool_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies_pool.json")
    urls, hwid = [], ""
    try:
        with open(pool_path, encoding="utf-8") as f:
            cfg = json.load(f)
        urls = [sub["url"] for sub in (cfg.get("subscriptions") or []) if sub.get("url")]
        hwid = str(cfg.get("hwid") or "")
    except (OSError, ValueError):
        pass
    nodes: list[S.VpnNode] = []
    for url in urls:
        try:
            nodes.extend(S.load_subscription(url, timeout=15.0, hwid=hwid))
        except Exception as exc:
            log(f"    подписка {url} не отдалась: {exc}")
    return S.dedupe(nodes)


def run_bridge(node: S.VpnNode) -> dict:
    # Ровно те же два шага, что делает VpnBridge.start: разбор ссылки,
    # подстановка IP вместо имени узла, сборка конфига.
    outbound = B.resolve_server(B.parse_link(node.raw) or {})
    if not outbound:
        return {"name": node.name, "tcp": "ссылка не разобралась", "udp": "—"}
    port = B.port_for(node.key)
    cfg = B.build_config(node.key, outbound, port)
    cfg["log"] = {"level": "debug", "timestamp": True}
    path = os.path.join(tempfile.gettempdir(), f"prove-{port}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    errpath = os.path.join(tempfile.gettempdir(), f"prove-{port}.err")
    errfile = open(errpath, "w", encoding="utf-8")
    log(f"  {node.name}  {node.address}:{node.port}  socks5://127.0.0.1:{port}")
    log(f"    server={outbound.get('server')} (узел {node.address})  "
        f"flow={outbound.get('flow') or '—'}  security={node.security}")
    proc = subprocess.Popen([B.find_binary(), "run", "-c", path],
                            stdout=errfile, stderr=subprocess.STDOUT)
    tcp = udp = "не проверено"
    try:
        deadline = time.monotonic() + 8.0
        opened = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if B.socks_alive(port, 0.5):
                opened = True
                break
            time.sleep(0.2)
        if not opened:
            tcp = udp = f"SOCKS-порт не открылся (код {proc.poll()})"
        else:
            tcp = tcp_real("127.0.0.1", port, target=GAME_HOST, tport=GAME_PORT,
                           timeout=TIMEOUT)
            udp = udp_real("127.0.0.1", port, timeout=TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        errfile.close()

    log(f"    TCP: {tcp}")
    log(f"    UDP: {udp}")
    with open(errpath, encoding="utf-8", errors="replace") as f:
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln).strip() for ln in f]
    errors = [ln for ln in lines if " ERROR " in ln or "FATAL" in ln]
    for ln in errors[-3:]:
        log(f"    sing-box: {ln[:200]}")
    return {"name": node.name, "address": f"{node.address}:{node.port}",
            "tcp": tcp, "udp": udp, "port": port, "log": errpath}


def main() -> int:
    binary = B.find_binary()
    log("== окружение ==")
    log("  sing-box: " + subprocess.run([binary, "version"], capture_output=True,
                                        text=True).stdout.strip().splitlines()[0])
    log("  Python-резолвер: deu.babay.su -> "
        + (socket.gethostbyname("deu.babay.su") if True else ""))

    log("== контроль: локальный SOCKS5 HAPP 127.0.0.1:10808 ==")
    log("  TCP: " + tcp_real("127.0.0.1", 10808, target=GAME_HOST, tport=GAME_PORT,
                             timeout=TIMEOUT))
    log("  UDP: " + udp_real("127.0.0.1", 10808, timeout=TIMEOUT))

    log("== узлы подписки через мост (sing-box + build_config) ==")
    if len(sys.argv) > 1:
        one = S.parse_link(sys.argv[1], "cli")
        nodes = [one] if one is not None else []
    else:
        nodes = fetch_nodes()
    log(f"  узлов получено: {len(nodes)}")
    rows = []
    seen = set()
    for node in nodes:
        if node.address in seen:
            continue
        seen.add(node.address)
        rows.append(run_bridge(node))

    log("== итог ==")
    for row in rows:
        tcp = "да" if row["tcp"].startswith("везёт TCP") else "нет"
        udp = "да" if row["udp"].startswith("везёт UDP") else "нет"
        log(f"  {row['address']:<22} TCP {tcp:<3} UDP {udp:<3}  "
            f"({row['tcp'][:60]} | {row['udp'][:60]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
