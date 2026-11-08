"""Стандартный offline-хендшейк RakNet: байты и полный цикл.

Сервер здесь поднимается свой, на 127.0.0.1: он отвечает ровно так, как
отвечает стандартный RakNet — 0x06 на 0x05, 0x08 на 0x07, 0x10 на 0x09.
Это проверка сборки и разбора пакетов и порядка шагов; живой сервер
проверяется отдельно:

    python3 tools/project_handshake.py arz
"""

import socket
import threading
import time
import unittest

from samp import raknet_offline as ro
from samp.raknet_offline import OfflineHandshake


class FakeRakNetServer(threading.Thread):
    """Мини-сервер: отвечает стандартной последовательностью RakNet."""

    daemon = True

    def __init__(self, replies=True, greet=True):
        super().__init__()
        self.replies = replies
        self.greet = greet
        self.seen = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.seen.append(data[0])
            if not self.replies:
                continue
            if data[0] == ro.ID_OPEN_CONNECTION_REQUEST_1:
                self.sock.sendto(bytes([ro.ID_OPEN_CONNECTION_REPLY_1])
                                 + b"\xAA" * 8 + b"\x00", addr)
            elif data[0] == ro.ID_OPEN_CONNECTION_REQUEST_2:
                self.sock.sendto(
                    bytes([ro.ID_OPEN_CONNECTION_REPLY_2]) + b"\xAA" * 8
                    + socket.inet_aton("127.0.0.1")
                    + (self.port).to_bytes(2, "big")
                    + (1400).to_bytes(2, "big") + b"\x00", addr)
            elif data[0] == ro.ID_CONNECTION_REQUEST:
                self.sock.sendto(
                    bytes([ro.ID_CONNECTION_REQUEST_ACCEPTED])
                    + socket.inet_aton("127.0.0.1") + (40000).to_bytes(2, "big")
                    + socket.inet_aton("127.0.0.1")
                    + (self.port).to_bytes(2, "big") + b"\xAA" * 8, addr)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class PacketBytesTests(unittest.TestCase):
    def test_request_1_length_and_version(self):
        packet = ro.build_request_1(version=5, mtu=1400, magic=False)
        self.assertEqual(packet[0], 0x05)
        self.assertEqual(packet[1], 5)
        self.assertEqual(len(packet), 1400)
        self.assertNotIn(ro.MAGIC, packet)

    def test_request_1_with_magic_keeps_mtu(self):
        packet = ro.build_request_1(version=6, mtu=1400, magic=True)
        self.assertTrue(packet.endswith(ro.MAGIC))
        self.assertEqual(len(packet), 1400)
        self.assertEqual(packet[1], 6)

    def test_request_2_layout(self):
        packet = ro.build_request_2("1.2.3.4", 5125, 1400, 0x1122334455667788)
        self.assertEqual(packet[0], 0x07)
        self.assertEqual(socket.inet_ntoa(packet[1:5]), "1.2.3.4")
        self.assertEqual(int.from_bytes(packet[5:7], "big"), 5125)
        self.assertEqual(int.from_bytes(packet[7:9], "big"), 1400)
        self.assertEqual(int.from_bytes(packet[9:17], "little"),
                         0x1122334455667788)

    def test_connection_request_layout(self):
        packet = ro.build_connection_request(0x1122334455667788)
        self.assertEqual(packet[0], 0x09)
        self.assertEqual(int.from_bytes(packet[1:9], "little"),
                         0x1122334455667788)

    def test_parse_reply_1(self):
        parsed = ro.parse_offline(bytes([0x06]) + b"\x11" * 8 + b"\x01")
        self.assertEqual(parsed["name"], "OPEN_CONNECTION_REPLY_1")
        self.assertTrue(parsed["use_security"])

    def test_parse_reply_2(self):
        raw = (bytes([0x08]) + b"\x22" * 8 + socket.inet_aton("1.2.3.4")
               + (5125).to_bytes(2, "big") + (1400).to_bytes(2, "big") + b"\x00")
        parsed = ro.parse_offline(raw)
        self.assertEqual(parsed["server_ip"], "1.2.3.4")
        self.assertEqual(parsed["server_port"], 5125)
        self.assertEqual(parsed["mtu"], 1400)

    def test_parse_unknown_and_empty(self):
        self.assertEqual(ro.parse_offline(b""), {})
        self.assertEqual(ro.parse_offline(b"\xff"), {"id": 255,
                                                     "name": "0xff",
                                                     "raw_len": 1})

    def test_guid_is_64_bit(self):
        self.assertLess(ro.make_guid(), 1 << 64)
        self.assertEqual(ro.make_guid(7), 7)


class HandshakeFlowTests(unittest.TestCase):
    def test_full_offline_handshake(self):
        server = FakeRakNetServer()
        server.start()
        try:
            report = OfflineHandshake("127.0.0.1", server.port,
                                      timeout=1.5, log=lambda *_: None).run()
        finally:
            server.stop()

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["mtu"], 1400)
        self.assertEqual(report["variant"], {"version": 5, "magic": False})
        names = [item["name"] for item in report["replies"]]
        self.assertIn("OPEN_CONNECTION_REPLY_1", names)
        self.assertIn("OPEN_CONNECTION_REPLY_2", names)
        self.assertIn("CONNECTION_REQUEST_ACCEPTED", names)
        self.assertIn(ro.ID_CONNECTION_REQUEST, server.seen)

    def test_silent_server_fails(self):
        server = FakeRakNetServer(replies=False)
        server.start()
        try:
            # Перебор вариантов выключен: вариант задан явно, иначе тест
            # висел бы на всех версиях подряд.
            engine = OfflineHandshake("127.0.0.1", server.port, timeout=0.3,
                                      log=lambda *_: None,
                                      variant={"version": 5, "magic": False})
            engine.timeout = 0.3
            report = engine.run()
        finally:
            server.stop()
        self.assertFalse(report["ok"])
        self.assertEqual(report["replies"], [])

    def test_explicit_variant_skips_scan(self):
        """Когда вариант известен, перебора нет: один REQUEST_1, не 22."""
        server = FakeRakNetServer()
        server.start()
        try:
            engine = OfflineHandshake("127.0.0.1", server.port, timeout=1.0,
                                      log=lambda *_: None,
                                      variant={"version": 5, "magic": False})
            report = engine.run()
        finally:
            time.sleep(0.1)
            server.stop()
        self.assertTrue(report["ok"])
        first_requests = [pid for pid in server.seen
                          if pid == ro.ID_OPEN_CONNECTION_REQUEST_1]
        self.assertLessEqual(len(first_requests), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
