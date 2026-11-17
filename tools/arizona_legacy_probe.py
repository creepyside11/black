#!/usr/bin/env python3
"""Check only the SA-MP/RakNet 3 offline handshake; no login is attempted.

Wire format informed by Newmcpe/RakClient (WTFPL), transport.rs. This is
an independent diagnostic implementation, not a complete RakNet client.
"""
import argparse
import socket
import struct
import time

OPEN_REQUEST = 24
OPEN_REPLY = 25
COOKIE = 26
COOKIE_XOR = 0x6969


def probe(host, port=7777, timeout=3.0):
    """Return a result describing only the offline exchange with one server."""
    if not 1 <= int(port) <= 65535 or not 0 < float(timeout) <= 30:
        raise ValueError('port must be 1..65535 and timeout must be >0 and <=30')
    address = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    deadline = time.monotonic() + timeout
    challenged = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(address)  # filter datagrams by sender
        sock.send(bytes((OPEN_REQUEST, 0, 0)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'status': 'timeout', 'cookie': challenged, 'address': address}
            sock.settimeout(remaining)
            try:
                data = sock.recv(4096)
            except socket.timeout:
                return {'status': 'timeout', 'cookie': challenged, 'address': address}
            if len(data) >= 3 and data[0] == COOKIE and not challenged:
                cookie = struct.unpack_from('<H', data, 1)[0]
                sock.send(bytes((OPEN_REQUEST,)) + struct.pack('<H', cookie ^ COOKIE_XOR))
                challenged = True
            elif len(data) >= 2 and data[0] == OPEN_REPLY:
                return {'status': 'offline_reply', 'cookie': challenged, 'address': address}


def main():
    parser = argparse.ArgumentParser(description='Проверка начального UDP-обмена SA-MP: это не вход в игру')
    parser.add_argument('host', help='IP или домен сервера')
    parser.add_argument('--port', type=int, default=7777)
    parser.add_argument('--timeout', type=float, default=3.0)
    args = parser.parse_args()
    try:
        result = probe(args.host, args.port, args.timeout)
    except (OSError, ValueError, IndexError) as exc:
        print(f'Ошибка проверки: {exc}')
        return 2
    if result['status'] == 'offline_reply':
        print('Получен offline-ответ SA-MP. Это НЕ означает вход или авторизацию.')
        print('Cookie:', 'получен' if result['cookie'] else 'не потребовался')
        return 0
    print('Ответ не получен в установленное время. Причину определить нельзя.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
