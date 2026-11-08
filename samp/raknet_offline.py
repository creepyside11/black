"""Стандартный offline-хендшейк RakNet — по образцу открытого RakSAMP.

Зачем отдельный модуль. У Black Russia свой первый пакет: клиент шлёт
ровно два байта b"BR", сервер отвечает своим OPEN_CONNECTION_REPLY и дальше
идёт обмен cookie. В стандартном RakNet (то есть в SA-MP и во всём, что от
него отпочковалось) первый шаг другой: OFFLINE-сообщение 0x05, ответ 0x06,
потом 0x07/0x08 и только затем 0x09 — запрос подключения. Эти пакеты идут
вне connected-фрейма, без message number и ack-битов, поэтому разобрать их
тем же парсером, что обычные датаграммы, нельзя.

Что здесь есть:
  build_request_1   — OFFLINE_MESSAGE_DATA_ID / OPEN_CONNECTION_REQUEST_1
  build_request_2   — OPEN_CONNECTION_REQUEST_2 (адрес сервера, MTU, GUID)
  build_connection_request — запрос подключения
  parse_offline     — разбор любого offline-ответа сервера
  scan_request_1    — перебор вариантов REQUEST_1 (версия и magic)

Формат взят из RakNet 3.x, который использует SA-MP: у REQUEST_1 нет magic
(он появился в RakNet 4), версия протокола лежит вторым байтом, а хвост
добивается нулями до заявленного MTU. Сервера на 4.x ждут magic — поэтому
scan_request_1 шлёт оба вида и смотрит, на какой придёт 0x06.
"""

from __future__ import annotations

import socket
import struct
import time

# Смещение и длина, проверенные по RakSAMP: имя канала не нужно, важна
# только длина нулевой добивки.
DEFAULT_MTU = 1400

# RakNet 4.x и SA-MP 0.3.DL ждут этот magic в конце offline-пакета.
MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")

# Версии протокола, которые встречаются в живых клиентах: 5 и 6 — SA-MP
# 0.3.7 и 0.3.DL, дальше — мобильные сборки и RakNet 4.x.
COMMON_VERSIONS = (5, 6, 7, 8, 9, 10, 11)

ID_OPEN_CONNECTION_REQUEST_1 = 0x05
ID_OPEN_CONNECTION_REPLY_1 = 0x06
ID_OPEN_CONNECTION_REQUEST_2 = 0x07
ID_OPEN_CONNECTION_REPLY_2 = 0x08
ID_CONNECTION_REQUEST = 0x09
ID_CONNECTION_REQUEST_ACCEPTED = 0x10
ID_CONNECTION_ATTEMPT_FAILED = 0x11
ID_ALREADY_CONNECTED = 0x12
ID_NO_FREE_INCOMING_CONNECTIONS = 0x13
ID_CONNECTION_BANNED = 0x17
ID_INVALID_PASSWORD = 0x18
ID_INCOMPATIBLE_PROTOCOL_VERSION = 0x19

OFFLINE_NAMES = {
    ID_OPEN_CONNECTION_REQUEST_1: "OPEN_CONNECTION_REQUEST_1",
    ID_OPEN_CONNECTION_REPLY_1: "OPEN_CONNECTION_REPLY_1",
    ID_OPEN_CONNECTION_REQUEST_2: "OPEN_CONNECTION_REQUEST_2",
    ID_OPEN_CONNECTION_REPLY_2: "OPEN_CONNECTION_REPLY_2",
    ID_CONNECTION_REQUEST: "CONNECTION_REQUEST",
    ID_CONNECTION_REQUEST_ACCEPTED: "CONNECTION_REQUEST_ACCEPTED",
    ID_CONNECTION_ATTEMPT_FAILED: "CONNECTION_ATTEMPT_FAILED",
    ID_ALREADY_CONNECTED: "ALREADY_CONNECTED",
    ID_NO_FREE_INCOMING_CONNECTIONS: "NO_FREE_INCOMING_CONNECTIONS",
    ID_CONNECTION_BANNED: "CONNECTION_BANNED",
    ID_INVALID_PASSWORD: "INVALID_PASSWORD",
    ID_INCOMPATIBLE_PROTOCOL_VERSION: "INCOMPATIBLE_PROTOCOL_VERSION",
}


def make_guid(seed: int | None = None) -> int:
    """Клиентский GUID. В RakNet это случайное 64-битное число."""
    if seed is None:
        import os
        seed = int.from_bytes(os.urandom(8), "little")
    return seed & 0xFFFFFFFFFFFFFFFF


def build_request_1(version: int = 5, mtu: int = DEFAULT_MTU,
                    magic: bool = False) -> bytes:
    """OPEN_CONNECTION_REQUEST_1.

    Без magic (RakNet 3.x, SA-MP 0.3.7):
        [0x05][version]  + нули до MTU
    С magic (RakNet 4.x, 0.3.DL):
        [0x05][version]  + нули до MTU + MAGIC
    """
    size = max(2, int(mtu)) if not magic else max(2, int(mtu) - len(MAGIC))
    body = bytes([ID_OPEN_CONNECTION_REQUEST_1, version & 0xFF])
    body += b"\x00" * (size - len(body))
    if magic:
        body += MAGIC
    return body


def build_request_2(server_ip: str, server_port: int, mtu: int,
                    guid: int, magic: bool = False) -> bytes:
    """OPEN_CONNECTION_REQUEST_2: адрес сервера, MTU, GUID клиента."""
    body = bytes([ID_OPEN_CONNECTION_REQUEST_2])
    body += socket.inet_aton(server_ip)
    body += struct.pack(">H", int(server_port) & 0xFFFF)
    body += struct.pack(">H", int(mtu) & 0xFFFF)
    body += struct.pack("<Q", guid & 0xFFFFFFFFFFFFFFFF)
    if magic:
        body += MAGIC
    return body


def build_connection_request(guid: int, security: bool = False,
                             password: bytes | None = None) -> bytes:
    """CONNECTION_REQUEST. Пароль (если есть) идёт после GUID."""
    body = bytes([ID_CONNECTION_REQUEST])
    body += struct.pack("<Q", guid & 0xFFFFFFFFFFFFFFFF)
    if security:
        body += b"\x01"
        if password:
            body += bytes([len(password)]) + password
        else:
            body += b"\x00"
    return body


def parse_offline(data: bytes) -> dict:
    """Разобрать offline-ответ сервера.

    Возвращает словарь с полями id/name и тем, что удалось прочитать:
    guid сервера, MTU, признак security, адрес. Неизвестный id тоже
    возвращается — по нему видно, что это вообще не RakNet.
    """
    if not data:
        return {}
    pid = data[0]
    out = {"id": pid, "name": OFFLINE_NAMES.get(pid, f"0x{pid:02x}"),
           "raw_len": len(data)}
    if pid == ID_OPEN_CONNECTION_REPLY_1 and len(data) >= 10:
        out["server_guid"] = struct.unpack("<Q", data[1:9])[0]
        out["use_security"] = bool(data[9])
        if len(data) > 10:
            # Хвост ответа — MTU-добивка сервера.
            out["tail"] = len(data) - 10
            out["mtu"] = len(data) - 10 + 2
    elif pid == ID_OPEN_CONNECTION_REPLY_2 and len(data) >= 18:
        out["server_guid"] = struct.unpack("<Q", data[1:9])[0]
        out["server_ip"] = socket.inet_ntoa(data[9:13])
        out["server_port"] = struct.unpack(">H", data[13:15])[0]
        out["mtu"] = struct.unpack(">H", data[15:17])[0]
        out["use_security"] = bool(data[17])
    elif pid == ID_CONNECTION_REQUEST_ACCEPTED and len(data) >= 17:
        out["client_ip"] = socket.inet_ntoa(data[1:5])
        out["client_port"] = struct.unpack(">H", data[5:7])[0]
        out["server_ip"] = socket.inet_ntoa(data[7:11])
        out["server_port"] = struct.unpack(">H", data[11:13])[0]
        out["server_guid"] = struct.unpack("<Q", data[13:21])[0] if len(data) >= 21 else None
    elif pid == ID_INCOMPATIBLE_PROTOCOL_VERSION and len(data) >= 2:
        out["server_version"] = data[1]
    elif pid == ID_CONNECTION_BANNED and len(data) >= 5:
        out["ban_reason"] = data[1:5].hex()
    return out


def scan_request_1(host: str, port: int, timeout: float = 2.0,
                   versions=COMMON_VERSIONS, log=print) -> dict:
    """Найти рабочий вариант REQUEST_1 перебором.

    Возвращает {"ok": bool, "variant": {...}, "reply": {...}} — либо
    причину, по которой сервер не ответил ни на один вариант.
    """
    attempts = []
    for version in versions:
        for magic in (False, True):
            attempts.append((version, magic))

    for version, magic in attempts:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        payload = build_request_1(version=version, magic=magic)
        try:
            sock.sendto(payload, (host, port))
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            log(f"  версия {version:>2} magic={int(magic)} — молчит")
            continue
        except OSError as exc:
            log(f"  версия {version:>2} magic={int(magic)} — сеть: {exc!r}")
            continue
        finally:
            sock.close()

        reply = parse_offline(data)
        log(f"  версия {version:>2} magic={int(magic)} — ответ "
            f"{len(data)}б id={reply.get('id'):#04x} ({reply.get('name')})")
        if reply.get("id") == ID_OPEN_CONNECTION_REPLY_1:
            return {"ok": True,
                    "variant": {"version": version, "magic": magic},
                    "reply": reply}
    return {"ok": False, "attempts": attempts,
            "reason": "ни один вариант REQUEST_1 не получил REPLY_1"}


class OfflineHandshake:
    """Полный standard-RakNet вход до CONNECTION_REQUEST_ACCEPTED.

    Класс держит один UDP-сокет: REPLY_2 сервер присылает на тот же
    source-порт, с которого пришёл REQUEST_2, и новый сокет его уже не
    получит. Поэтому шаги идут подряд, а не отдельными сокетами, как в
    scan_request_1 — там каждый вариант проверяется независимо.
    """

    def __init__(self, host: str, port: int, guid: int | None = None,
                 timeout: float = 3.0, log=print, transport=None,
                 variant: dict | None = None):
        self.host = host
        self.port = int(port)
        self.guid = make_guid(guid)
        self.timeout = timeout
        self.log = log
        # Свой UDP-сокет нужен только когда класс запускают отдельно от
        # бота (инструменты). Сессия передаёт сюда свой транспорт — тогда
        # offline-этап идёт по тому же каналу, включая прокси.
        self.transport = transport
        self.variant = dict(variant or {})
        self.server_guid = None
        self.mtu = DEFAULT_MTU
        self.sock = None
        self.history = []

    # --- транспорт ---------------------------------------------------

    def _ensure_sock(self) -> None:
        if self.transport is not None or self.sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.05)
        self.sock = sock

    def _send(self, payload: bytes) -> None:
        if self.transport is not None:
            self.transport.send(payload)
            return
        self._ensure_sock()
        self.sock.sendto(payload, (self.host, self.port))

    def _recv_wait(self, timeout: float | None = None) -> bytes | None:
        """Ждать ответ не дольше таймаута.

        Транспорт бота неблокирующий: recv() возвращает None сразу, как
        только в буфере пусто. Поэтому ожидание — цикл с мелкой паузой, а
        не один вызов, иначе ответ сервера, пришедший через 20 мс, был бы
        принят за молчание.
        """
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + max(0.05, float(limit))
        while True:
            data = None
            if self.transport is not None:
                data = self.transport.recv(4096)
            else:
                self._ensure_sock()
                try:
                    data, _ = self.sock.recvfrom(4096)
                except socket.timeout:
                    data = None
                except OSError as exc:
                    self.log(f"[OFFLINE] сеть: {exc!r}")
                    return None
            if data:
                self.history.append(parse_offline(data))
                return data
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def _exchange(self, payload: bytes, expect: int,
                  attempts: int = 3) -> dict | None:
        """Отправить пакет и дождаться ответа нужного типа.

        UDP теряется, поэтому повторяем: сервер отвечает на каждый
        REQUEST_1 заново, и вторая попытка обычно проходит.
        """
        for attempt in range(1, max(1, attempts) + 1):
            self._send(payload)
            data = self._recv_wait()
            if not data:
                continue
            reply = parse_offline(data)
            if reply.get("id") == expect:
                return reply
            # Чужой offline-ответ — не повод молчать: сервер мог прислать
            # отказ (бан, нет мест). Показываем его как есть.
            self.log(f"[OFFLINE] вместо {OFFLINE_NAMES.get(expect)} пришло "
                     f"{reply.get('name')}")
            return None
        return None

    def open(self) -> bool:
        """Шаг 1-2: REQUEST_1 -> REPLY_1. True, если сервер стандартный."""
        if not self.variant:
            if self.transport is not None:
                # Внутри бота перебор не нужен и вреден: каждый вариант
                # шлётся своим сокетом, а тут канал один. Берём версию по
                # умолчанию — она же проверяется инструментом.
                self.variant = {"version": 5, "magic": False}
            else:
                found = scan_request_1(self.host, self.port,
                                       timeout=self.timeout, log=self.log)
                if not found.get("ok"):
                    self.log(f"[OFFLINE] {found.get('reason')}")
                    return False
                self.variant = found["variant"]
                self.log(f"[OFFLINE] сервер стандартный: протокол "
                         f"{self.variant.get('version')}, "
                         f"magic={int(bool(self.variant.get('magic')))}")

        # Дальше всё идёт одним каналом. REPLY_2 сервер шлёт на тот
        # source-порт, с которого пришёл REQUEST_2, поэтому переборная
        # проверка выше (у неё свой сокет на каждый вариант) годится только
        # для разведки — здесь REQUEST_1 повторяется уже на рабочем канале.
        reply = self._exchange(
            build_request_1(version=self.variant.get("version", 5),
                            magic=bool(self.variant.get("magic"))),
            ID_OPEN_CONNECTION_REPLY_1,
        )
        if not reply:
            self.log("[OFFLINE] нет REPLY_1")
            return False
        self.server_guid = reply.get("server_guid")
        self.mtu = reply.get("mtu") or DEFAULT_MTU
        return True

    def connect_request(self) -> bool:
        """Шаг 3-4: REQUEST_2 -> REPLY_2, затем CONNECTION_REQUEST -> ACCEPTED."""
        reply = self._exchange(
            build_request_2(self.host, self.port, self.mtu, self.guid,
                            magic=bool(self.variant.get("magic"))),
            ID_OPEN_CONNECTION_REPLY_2,
        )
        if not reply:
            self.log("[OFFLINE] нет REPLY_2")
            return False
        self.mtu = reply.get("mtu") or self.mtu
        self.log(f"[OFFLINE] REPLY_2: mtu={self.mtu}")

        reply = self._exchange(build_connection_request(self.guid),
                               ID_CONNECTION_REQUEST_ACCEPTED)
        if not reply:
            self.log("[OFFLINE] нет CONNECTION_REQUEST_ACCEPTED")
            return False
        self.log(f"[OFFLINE] подключение принято сервером "
                 f"{reply.get('server_ip')}:{reply.get('server_port')}")
        return True

    def run(self) -> dict:
        """Весь offline-этап. Возвращает отчёт для калибровки."""
        started = time.time()
        ok = False
        try:
            if self.open():
                ok = self.connect_request()
        finally:
            # Свой сокет закрываем, чужой транспорт — нет: он принадлежит
            # сессии и нужен ей для connected-фазы.
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
        return {
            "ok": ok,
            "host": self.host,
            "port": self.port,
            "variant": dict(self.variant),
            "server_guid": self.server_guid,
            "mtu": self.mtu,
            "client_guid": self.guid,
            "replies": self.history,
            "seconds": round(time.time() - started, 2),
        }
