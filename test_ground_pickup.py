"""Поднятие предметов с земли, офлайн.

Главная проверка здесь — не логика ходьбы, а ПАКЕТ. Тело 18 снято с
живого клиента, и если оно поедет, ни один предмет не поднимется.
"""

import json
import os
import tempfile
import unittest

from logic import ground_pickup as gp
from samp.constants import RPC
from samp.packets import PacketBuilder


GRANT = {"o": 1, "t": 5, "i": "Вы получили Металлолом", "d": 10, "s": 22, "k": ">>", "b": 0}
NOT_GRANT = {"o": 1, "t": 3, "i": "Материалы взяты.", "d": 3, "s": -1, "b": 0}
OTHER_ITEM = {"o": 1, "t": 5, "i": "Вы получили 500 рублей", "d": 10, "s": 22, "b": 0}


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, s):
        self.now += float(s)


class FakeApi:
    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.pos = tuple(pos)
        self.calls = 0

    def getbotposition(self):
        return self.pos

    def pickup_ground(self):
        self.calls += 1
        return True


class FakeBot:
    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.api = FakeApi(pos)


def make(bot, **kw):
    kw.setdefault("log", lambda m: None)
    return gp.GroundPickup(bot, **kw)


def walker(bot, target):
    bot.api.pos = tuple(target)


def planned(stand):
    return [e["payload"] for e in stand.journal if e["kind"] in ("sent", "would_send")]


class ByteTests(unittest.TestCase):
    def test_body_is_the_live_constant(self):
        out = PacketBuilder.rpc_build("picked_up_interact", 18)
        body = out[0] if isinstance(out, tuple) else out
        self.assertEqual(body.hex(), "0b9df10090000000")

    def test_constant_is_eighteen(self):
        self.assertEqual(RPC.GROUND_PICKUP_CONST, 18)

    def test_pickup_id_would_be_a_different_packet(self):
        # Если кто-то подсунет id предмета вместо константы — тело другое.
        a = PacketBuilder.rpc_build("picked_up_interact", 18)
        b = PacketBuilder.rpc_build("picked_up_interact", 2049)
        ha = (a[0] if isinstance(a, tuple) else a).hex()
        hb = (b[0] if isinstance(b, tuple) else b).hex()
        self.assertNotEqual(ha, hb)


class GrantTests(unittest.TestCase):
    def test_recognises_the_real_grant(self):
        self.assertTrue(gp.is_grant(13, GRANT))

    def test_ignores_other_notifications(self):
        self.assertFalse(gp.is_grant(13, NOT_GRANT))
        self.assertFalse(gp.is_grant(13, {"o": 1, "t": 5}))
        self.assertFalse(gp.is_grant(31, GRANT))

    def test_any_grant_notice_counts_as_an_answer(self):
        # Деньги тоже считаем ответом: сервер сказал «вы получили», значит
        # нажатие дошло. Различать предмет и деньги по тексту — гадание.
        self.assertTrue(gp.is_grant(13, OTHER_ITEM))

    def test_survives_garbage(self):
        self.assertFalse(gp.is_grant(13, None))
        self.assertFalse(gp.is_grant(13, "чепуха"))
        self.assertFalse(gp.is_grant("не число", GRANT))


class RouteTests(unittest.TestCase):
    def test_loads_and_thins(self):
        rows = [[0.0, 0.0, 0.0, 0.0], [1.0, 0.5, 0.0, 0.0],
                [2.0, 10.0, 0.0, 0.0], [3.0, 10.5, 0.0, 0.0]]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(rows, fh)
            path = fh.name
        try:
            pts = gp.load_route(path)
        finally:
            os.unlink(path)
        self.assertEqual(pts[0], (0.0, 0.0, 0.0))
        self.assertIn((10.0, 0.0, 0.0), pts)
        self.assertLess(len(pts), len(rows))

    def test_thin_keeps_the_last_point(self):
        pts = [(0, 0, 0), (0.1, 0, 0), (0.2, 0, 0)]
        self.assertEqual(gp.thin_route(pts)[-1], (0.2, 0.0, 0.0))

    def test_thin_of_empty_is_empty(self):
        self.assertEqual(gp.thin_route([]), [])

    def test_real_route_file_loads(self):
        path = "_diag/route_arzamas_farm.json"
        if not os.path.exists(path):
            self.skipTest("нет файла маршрута")
        pts = gp.load_route(path)
        self.assertGreater(len(pts), 10)
        for p in pts:
            self.assertEqual(len(p), 3)


class DisarmedTests(unittest.TestCase):
    def test_sends_nothing(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=False)
        stand.start()
        stand.step()
        self.assertEqual(bot.api.calls, 0)

    def test_but_says_what_it_would_send(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=False)
        stand.start()
        stand.step()
        self.assertTrue(any("тело 18" in t for t in gp_texts(stand)))

    def test_does_not_hang_on_the_same_point(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=False)
        stand.start()
        for _ in range(3):
            stand.step()
        self.assertEqual(stand.state, gp.S_DONE)


def gp_texts(stand):
    return [e["text"] for e in stand.journal]


class ArmedTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.real = gp.time
        gp.time = self.clock

    def tearDown(self):
        gp.time = self.real

    def test_sends_one_packet_on_the_point(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=True)
        stand.start()
        stand.step()
        self.assertEqual(bot.api.calls, 1)
        self.assertEqual(stand.state, gp.S_WAIT)
        self.assertEqual(stand.attempts, 1)

    def test_grant_closes_the_point(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=True)
        stand.start()
        stand.step()
        stand.on_interface(gp.INTERFACE_NOTIFY, GRANT)
        self.assertEqual(stand.grants, 1)
        self.assertEqual(stand.state, gp.S_TRAVEL)
        self.assertEqual(stand.last_grant, "Вы получили Металлолом")

    def test_walks_then_picks(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(20.0, 0.0, 0.0)], armed=True, travel=walker)
        stand.start()
        first = stand.step()
        self.assertIn("иду", first)
        self.assertEqual(bot.api.calls, 0)
        stand.step()
        self.assertEqual(bot.api.calls, 1)

    def test_no_travel_reports_honestly(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(50.0, 0.0, 0.0)], armed=True)
        stand.start()
        self.assertIn("нет транспорта", stand.step())
        self.assertEqual(bot.api.calls, 0)

    def test_timeout_retries_then_gives_up(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[(0.0, 0.0, 0.0)], armed=True, retry_limit=2)
        stand.start()
        stand.step()                       # попытка 1
        self.clock.advance(5.0)
        stand.step()                       # таймаут -> попытка ещё
        self.assertEqual(stand.misses, 1)
        stand.step()                       # попытка 2
        self.assertEqual(bot.api.calls, 2)
        self.clock.advance(5.0)
        stand.step()                       # таймаут -> сдаёмся
        self.assertEqual(stand.misses, 2)
        self.assertIn("не далась", " ".join(gp_texts(stand)))

    def test_pair_uses_every_route_point_once(self):
        route = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (20.0, 0.0, 0.0)]
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=route, armed=True, travel=walker)
        stand.start()
        for _ in range(20):
            if stand.state == gp.S_DONE:
                break
            stand.step()
            if stand.state == gp.S_WAIT:
                stand.on_interface(gp.INTERFACE_NOTIFY, GRANT)
        self.assertEqual(stand.grants, 3)
        self.assertEqual(bot.api.calls, 3)
        self.assertEqual(stand.state, gp.S_DONE)


class HuntTests(unittest.TestCase):
    def test_picks_the_nearest_object(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, mode="hunt", armed=True)
        stand.on_create_pickup(1, 1314, (30.0, 0.0, 0.0))
        stand.on_create_pickup(2, 1314, (2.0, 0.0, 0.0))
        stand.start()
        stand.step()
        self.assertEqual(stand.sent_key, "obj:2")

    def test_ignores_objects_out_of_range(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, mode="hunt", armed=True, hunt_radius=10.0)
        stand.on_create_pickup(1, 1314, (500.0, 0.0, 0.0))
        stand.start()
        stand.step()          # только шаг двигает автомат, не start()
        self.assertEqual(stand.state, gp.S_DONE)

    def test_parses_create_pickup_packet(self):
        import struct

        payload = struct.pack("<iii", 2049, 1314, 2) + struct.pack("<fff", 1.0, 2.0, 3.0)
        bot = FakeBot()
        stand = make(bot)
        stand.on_packet(None, {"rpc_id": gp.RPC_CREATE_PICKUP, "payload": payload})
        self.assertIn(2049, stand.objects)
        self.assertEqual(stand.objects[2049]["pos"], (1.0, 2.0, 3.0))

    def test_short_create_pickup_is_ignored(self):
        stand = make(FakeBot())
        stand.on_packet(None, {"rpc_id": gp.RPC_CREATE_PICKUP, "payload": b"\x01\x02"})
        self.assertEqual(stand.objects, {})

    def test_route_falls_back_to_hunting(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make(bot, route=[], armed=True)
        stand.on_create_pickup(7, 1314, (1.0, 0.0, 0.0))
        stand.start()
        stand.step()
        self.assertEqual(stand.sent_key, "obj:7")


class ReportTests(unittest.TestCase):
    def test_empty(self):
        stand = make(FakeBot())
        stand.start()
        self.assertEqual(stand.report()["вердикт"], "недостаточно данных")

    def test_nothing_worked(self):
        stand = make(FakeBot(), armed=True, route=[(0.0, 0.0, 0.0)])
        stand.start()
        stand.step()
        stand.on_interface(gp.INTERFACE_NOTIFY, NOT_GRANT)
        stand._resolve(False)
        self.assertIn("не сработало", stand.report()["вердикт"])

    def test_all_worked(self):
        stand = make(FakeBot(), armed=True, route=[(0.0, 0.0, 0.0)])
        stand.start()
        stand.step()
        stand.on_interface(gp.INTERFACE_NOTIFY, GRANT)
        self.assertIn("работает", stand.report()["вердикт"])
        self.assertEqual(stand.report()["поднято"], 1)

    def test_journal_renders(self):
        stand = make(FakeBot(), route=[(0.0, 0.0, 0.0)])
        stand.start()
        stand.step()
        dump = stand.dump_journal()
        self.assertIn("СБОР С ЗЕМЛИ", dump)

    def test_status_keys(self):
        stand = make(FakeBot())
        stand.start()
        for key in ("state", "armed", "попыток", "поднято", "осталось точек"):
            self.assertIn(key, stand.status())


class ListenerTests(unittest.TestCase):
    def test_emitter_signature(self):
        stand = make(FakeBot())
        stand.start()
        stand.on_receive_json(object(), gp.INTERFACE_NOTIFY, GRANT)
        self.assertEqual(stand.grants, 1)

    def test_stop_is_idempotent(self):
        stand = make(FakeBot())
        stand.start()
        stand.stop("тест")
        stand.stop("тест")
        self.assertEqual(stand.state, gp.S_OFF)
        self.assertEqual(stand.step(), "сборщик выключен")

    def test_describe_survives_garbage(self):
        self.assertIsInstance(gp.describe(None), str)
        self.assertIsInstance(gp.describe({"c": 1}), str)
        self.assertIsInstance(gp.describe({"i": "Вы получили X"}), str)


class PollTests(unittest.TestCase):
    """Объекты из потока сессии и исчезновение предмета."""

    def _bot(self, picks):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        bot.session = type("S", (), {"stream": FakeStream(picks)})()
        return bot

    def test_takes_objects_from_the_stream(self):
        # 2.24 м от бота — ближе pickup_range, поэтому жмём сразу
        bot = self._bot({2049: {"x": 1.0, "y": 2.0, "z": 0.0}})
        stand = make(bot, mode="hunt", armed=True)
        stand.start()
        stand.step()
        self.assertIn(2049, stand.objects)
        self.assertEqual(stand.objects[2049]["pos"], (1.0, 2.0, 0.0))
        self.assertEqual(stand.sent_key, "obj:2049")

    def test_gone_object_after_a_press_counts_as_taken(self):
        picks = {2049: {"x": 1.0, "y": 0.0, "z": 0.0}}
        bot = self._bot(picks)
        stand = make(bot, mode="hunt", armed=True)
        stand.start()
        stand.step()
        self.assertEqual(stand.sent_key, "obj:2049")
        picks.clear()                      # предмет исчез из мира
        stand.step()
        self.assertIn("obj:2049", stand.taken)
        self.assertEqual(stand.objects, {})

    def test_gone_object_without_a_press_is_ignored(self):
        picks = {2049: {"x": 1.0, "y": 0.0, "z": 0.0}}
        bot = self._bot(picks)
        stand = make(bot, mode="hunt", armed=True)
        stand.start()
        stand.step()
        stand._poll_objects()              # объект есть
        picks.clear()
        stand.failed.clear()
        stand.tries.pop("obj:2049", None)
        stand._poll_objects()
        self.assertNotIn("obj:2049", stand.taken)

    def test_taken_object_is_never_targeted_again(self):
        bot = self._bot({2049: {"x": 1.0, "y": 0.0, "z": 0.0}})
        stand = make(bot, mode="hunt", armed=True)
        stand.start()
        stand.step()
        stand.on_interface(gp.INTERFACE_NOTIFY, GRANT)
        stand.step()
        self.assertEqual(stand.state, gp.S_DONE)

    def test_broken_stream_does_not_crash(self):
        bot = FakeBot()
        bot.session = type("S", (), {"stream": None})()
        stand = make(bot, mode="hunt", armed=True)
        stand.start()
        stand.step()                        # get_all_pickups упадёт — ловим
        self.assertEqual(stand.objects, {})

    def test_on_receive_rpc_signature(self):
        import struct

        payload = struct.pack("<iii", 7, 1314, 2) + struct.pack("<fff", 5.0, 6.0, 7.0)
        stand = make(FakeBot())
        stand.on_receive_rpc(None, gp.RPC_CREATE_PICKUP, payload)
        self.assertIn(7, stand.objects)
        stand.on_receive_rpc(None, 999, payload)
        self.assertEqual(len(stand.objects), 1)


class FakeStream:
    def __init__(self, picks=None):
        self.picks = picks if picks is not None else {}

    def get_all_pickups(self):
        return self.picks


if __name__ == "__main__":
    unittest.main(verbosity=2)
