"""Завод, офлайн: стенд против настоящих кадров из захвата.

Кадры ниже — не выдумка, а дословная последовательность из
_diag/factory.pcap, сессия 31.10 11:17–11:21. Если стенд перестанет их
распознавать, значит протокол поехал.

Стенд не боевой модуль: он обязан НИЧЕГО не отправлять без armed=True.
Это тоже проверяется здесь.
"""

import time
import unittest

from logic import factory as fac


# --- настоящие кадры из захвата ---------------------------------------------

DIALOG_OPEN = {
    "o": 1,
    "n": "Прораб - Жора",
    "d": "Ну здарова! Крепыши нам нужны, давай я тебе устрою экскурсию?",
    "m": 260,
    "ts": 100,
    "st": 1,
    "b": [{"b": 1}],
}
DIALOG_PAGES = [
    {"t": 1, "n": "Прораб - Жора", "d": "Во-первых, тебе нужно переодеться.", "m": 260},
    {"t": 1, "n": "Прораб - Жора", "d": "Здесь тебе и предстоит попотеть.", "m": 260},
    {"t": 1, "n": "Прораб - Жора", "d": "После того как Вы взяли сырье, отнесите его к станку.", "m": 260},
    {"t": 1, "n": "Прораб - Жора", "d": "После того как сделали материал, его нужно отнести на ленту сдачи.", "m": 260},
    {"t": 1, "n": "Прораб - Жора", "d": "Ладно... Думаю я тебе все доходчиво объяснил.", "m": 260},
]
DIALOG_CLOSE = {"c": 1}

SLIDER_A = {
    "o": 1, "g": 1, "p_max": 50, "p_max_bar": 100, "p_start": 0,
    "p_tick": 1, "p_timer": 80, "p_title": "Работа с материалом", "x": 1,
}
SLIDER_B = {
    "o": 1, "g": 1, "p_max": 100, "p_max_bar": 100, "p_start": 50,
    "p_tick": 1, "p_timer": 80, "p_title": "Работа с материалом", "x": 1,
}
FINGER = {"o": 1, "g": 2}

MATERIALS_TAKEN = {"o": 1, "t": 3, "i": "Материалы взяты.", "d": 3, "s": -1, "b": 0}
GOT_ORE = {
    "o": 1, "t": 5, "i": "Вы получили Металлолом", "d": 10, "s": 22, "k": ">>", "b": 0,
}
GOAL = {"o": 1, "m": 5, "t": 4, "mq": "Переоденьтесь в рабочую форму", "mt": 0}


def full_session():
    """Один полный цикл, как он шёл в захвате."""
    frames = [(fac.DIALOG_INTERFACE, DIALOG_OPEN)]
    frames += [(fac.DIALOG_INTERFACE, p) for p in DIALOG_PAGES]
    frames += [
        (fac.DIALOG_INTERFACE, DIALOG_CLOSE),
        (fac.GOAL_INTERFACE, GOAL),
        (fac.NOTIFY_INTERFACE, MATERIALS_TAKEN),
        (fac.MINIGAME_INTERFACE, SLIDER_A),
        (fac.MINIGAME_INTERFACE, FINGER),
        (fac.MINIGAME_INTERFACE, SLIDER_B),
        (fac.MINIGAME_INTERFACE, FINGER),
        (fac.NOTIFY_INTERFACE, GOT_ORE),
    ]
    return frames


# --- заглушки ---------------------------------------------------------------

class FakeClock:
    """Управляемые часы: без них тесты спали бы на реальных паузах."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)

    def strftime(self, *a, **k):
        return time.strftime(*a, **k)

    def localtime(self, *a, **k):
        return time.localtime(*a, **k)


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, payload, rpc_name="", *a, **k):
        self.sent.append((rpc_name, payload))
        return True


class FakeSession:
    def __init__(self):
        self.connected = True
        self.json_sent = []
        self._sender = FakeSender()

    def send_json(self, interface_id, payload, packet_name=""):
        self.json_sent.append((interface_id, dict(payload)))
        return True


class FakeApi:
    def __init__(self, pos=fac.LOCKER):
        self.pos = pos

    def getbotposition(self):
        return self.pos


class FakeBot:
    def __init__(self, pos=fac.LOCKER):
        self.session = FakeSession()
        self.api = FakeApi(pos)


def make_stand(bot, **kwargs):
    kwargs.setdefault("log", lambda m: None)
    return fac.FactoryTester(bot, **kwargs)


def feed(stand, frames):
    for iid, data in frames:
        stand.on_interface(iid, data)


def sent_json(bot):
    return [(iid, tuple(sorted(p.items()))) for iid, p in bot.session.json_sent]


def planned(stand):
    """Что стенд отправил бы: и в боевом режиме, и в холостом."""
    return [e["payload"] for e in stand.journal if e["kind"] in ("queued", "would_send")]


def planned_texts(stand):
    return [e["text"] for e in stand.journal if e["kind"] in ("queued", "would_send")]


def step_until(stand, predicate, limit=12):
    """Крутить step(), пока условие не выполнится. Возвращает число шагов."""
    for i in range(limit):
        if predicate():
            return i
        stand.step()
    return limit


def at_locker(stand):
    """Довести стенд до «диалог пройден, стоим на раздевалке»."""
    stand.start()
    stand.dialog_closed = True
    stand.step()
    return stand


def _walker(bot, target):
    """Транспорт для тестов: сразу переносит бота в точку."""
    bot.api.pos = target
    return target


# --- тесты ------------------------------------------------------------------

class FormulaTests(unittest.TestCase):
    def test_delay_for_both_phases(self):
        # (50 - 0) * 80 / 1 = 4000 мс, и (100 - 50) * 80 / 1 = 4000 мс
        self.assertEqual(fac.slider_delay_ms(SLIDER_A), 4000.0)
        self.assertEqual(fac.slider_delay_ms(SLIDER_B), 4000.0)

    def test_delay_scales_with_tick(self):
        fast = dict(SLIDER_A, p_tick=2)
        self.assertEqual(fac.slider_delay_ms(fast), 2000.0)

    def test_delay_none_without_timer(self):
        self.assertIsNone(fac.slider_delay_ms({"g": 1}))
        self.assertIsNone(fac.slider_delay_ms({"p_timer": 0}))

    def test_delay_zero_when_start_at_max(self):
        self.assertEqual(fac.slider_delay_ms(dict(SLIDER_A, p_start=50)), 0.0)

    def test_mode_parsing(self):
        self.assertEqual(fac.minigame_mode(SLIDER_A), 1)
        self.assertEqual(fac.minigame_mode(FINGER), 2)
        self.assertIsNone(fac.minigame_mode({"o": 1}))


class RouteTests(unittest.TestCase):
    def test_route_is_inside_one_interior(self):
        heights = {name: point[2] for name, point in fac.ROUTE_NAMES.items()}
        for name, z in heights.items():
            self.assertAlmostEqual(z, 1381.1, delta=0.5, msg=name)

    def test_route_points_close_together(self):
        # Завод маленький: самая длинная перебежка в захвате была около 30 м.
        import math

        for a, pa in fac.ROUTE_NAMES.items():
            for b, pb in fac.ROUTE_NAMES.items():
                self.assertLess(math.dist(pa, pb), 50.0, f"{a} -> {b}")

    def test_object_ids(self):
        self.assertEqual(fac.OBJ_LOCKER, 2048)
        self.assertEqual(fac.OBJ_MATERIAL, 2049)
        self.assertEqual(fac.RPC_INTERACT, 436)


class DisarmedTests(unittest.TestCase):
    """По умолчанию стенд обязан молчать."""

    def test_full_session_sends_nothing(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=False)
        stand.start()
        feed(stand, full_session())
        for _ in range(4):
            stand.step()
        self.assertEqual(bot.session.json_sent, [])
        self.assertEqual(bot.session._sender.sent, [])

    def test_but_records_what_it_would_send(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=False)
        stand.start()
        feed(stand, full_session())
        stand.step()
        kinds = [e["kind"] for e in stand.journal]
        self.assertIn("would_send", kinds)
        would = [e["text"] for e in stand.journal if e["kind"] == "would_send"]
        self.assertTrue(any("iid=63" in t for t in would))
        self.assertTrue(any("iid=31" in t for t in would))

    def test_interact_is_not_sent(self):
        bot = FakeBot(pos=fac.LOCKER)
        stand = make_stand(bot, armed=False)
        stand.start()
        stand.dialog_closed = True
        stand.step()  # раздевалка: уже на месте -> взаимодействие
        self.assertEqual(bot.session._sender.sent, [])


class DialogTests(unittest.TestCase):
    def test_counts_every_page(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, [(fac.DIALOG_INTERFACE, DIALOG_OPEN)])
        feed(stand, [(fac.DIALOG_INTERFACE, p) for p in DIALOG_PAGES])
        self.assertEqual(stand.dialog_pages, 6)
        self.assertFalse(stand.dialog_closed)

    def test_close_is_recognised(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, [(fac.DIALOG_INTERFACE, DIALOG_CLOSE)])
        self.assertTrue(stand.dialog_closed)
        self.assertEqual(stand.dialog_pages, 0)

    def test_one_answer_per_page(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, full_session())
        answers = [
            e for e in stand.journal
            if e["kind"] == "would_send" and e["payload"] == fac.DIALOG_NEXT
        ]
        self.assertEqual(len(answers), 6)

    def test_advances_after_close(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, full_session())
        stand.step()
        self.assertEqual(stand.state, fac.S_LOCKER)


class MinigameTests(unittest.TestCase):
    def test_slider_answered_with_t(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        self.assertIn(fac.SLIDER_DONE, planned(stand))

    def test_finger_answered_with_s(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A), (fac.MINIGAME_INTERFACE, FINGER)])
        self.assertIn(fac.HIT, planned(stand))

    def test_counters(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, full_session())
        self.assertEqual(stand.sliders, 2)
        self.assertEqual(stand.hits, 2)
        self.assertEqual(stand.misses, 0)

    def test_miss_policy(self):
        bot = FakeBot()
        stand = make_stand(bot, miss_every=2)
        stand.start()
        feed(stand, full_session())
        self.assertEqual(stand.hits, 1)
        self.assertEqual(stand.misses, 1)
        self.assertIn(fac.MISS, planned(stand))

    def test_timing_off_queues_nothing_for_slider(self):
        bot = FakeBot()
        stand = make_stand(bot, timing=fac.TIMING_OFF)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        queued = [e for e in stand.journal if e["kind"] == "queued"]
        self.assertEqual(queued, [])
        skipped = [e for e in stand.journal if e["kind"] == "skip"]
        self.assertEqual(len(skipped), 1)

    def test_instant_policy_waits_nothing(self):
        bot = FakeBot()
        stand = make_stand(bot, timing=fac.TIMING_INSTANT)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        self.assertTrue(any("через 0 мс" in t for t in planned_texts(stand)))

    def test_natural_policy_adds_overhead(self):
        bot = FakeBot()
        stand = make_stand(bot, timing=fac.TIMING_NATURAL, overhead=0.6)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        # 4000 мс формулы + 600 мс накладных = 4600 мс
        self.assertTrue(any("через 4600 мс" in t for t in planned_texts(stand)))


class ArmedTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.real_time = fac.time
        fac.time = self.clock

    def tearDown(self):
        fac.time = self.real_time

    def test_dialog_answers_reach_the_session(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=True)
        stand.start()
        feed(stand, [(fac.DIALOG_INTERFACE, DIALOG_OPEN)])
        self.clock.advance(1.0)
        stand.step()
        self.assertEqual(sent_json(bot), [(fac.DIALOG_INTERFACE, (("bk", 0),))])

    def test_slider_answer_reaches_the_session(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=True, timing=fac.TIMING_INSTANT)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A), (fac.MINIGAME_INTERFACE, FINGER)])
        self.clock.advance(1.0)
        stand.step()
        sent = sent_json(bot)
        self.assertIn((fac.MINIGAME_INTERFACE, (("t", 1),)), sent)
        self.assertIn((fac.MINIGAME_INTERFACE, (("s", 1),)), sent)

    def test_natural_timing_holds_the_answer_back(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=True, timing=fac.TIMING_NATURAL, overhead=0.6)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        self.clock.advance(1.0)
        stand.step()
        self.assertEqual(bot.session.json_sent, [])
        self.clock.advance(4.0)  # всего 5 с > 4.6 с
        stand.step()
        self.assertEqual(sent_json(bot), [(fac.MINIGAME_INTERFACE, (("t", 1),))])

    def test_interact_uses_rpc_436(self):
        bot = FakeBot(pos=fac.LOCKER)
        stand = make_stand(bot, armed=True)
        at_locker(stand)
        stand.step()  # взаимодействие с раздевалкой
        self.assertEqual(len(bot.session._sender.sent), 1)
        rpc_name, payload = bot.session._sender.sent[0]
        self.assertEqual(rpc_name, "picked_up_pickup")
        # Тело снято с провода: 436 с id 2048 в _diag/factory.pcap
        self.assertEqual(payload.hex(), "0bb4f10000400000")


class MovementTests(unittest.TestCase):
    def test_travel_is_called_when_far(self):
        moves = []
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make_stand(bot, travel=lambda b, t: moves.append(t))
        at_locker(stand)
        stand.step()
        self.assertEqual(moves, [fac.LOCKER])

    def test_no_travel_when_already_there(self):
        moves = []
        bot = FakeBot(pos=fac.LOCKER)
        stand = make_stand(bot, travel=lambda b, t: moves.append(t))
        at_locker(stand)
        stand.step()
        self.assertEqual(moves, [])

    def test_missing_travel_does_not_crash(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        stand = make_stand(bot)
        at_locker(stand)
        text = stand.step()
        self.assertIn("нет транспорта", text)


class JournalTests(unittest.TestCase):
    def test_dump_journal_renders(self):
        bot = FakeBot()
        stand = make_stand(bot)
        stand.start()
        feed(stand, full_session())
        dumped = stand.dump_journal()
        self.assertTrue(dumped)
        self.assertIn("iid=63", dumped)
        self.assertIn("iid=31", dumped)

    def test_status_shape(self):
        stand = make_stand(FakeBot())
        stand.start()
        status = stand.status()
        for key in ("state", "armed", "полос", "удач", "промахов"):
            self.assertIn(key, status)

    def test_probe_report_without_data(self):
        stand = make_stand(FakeBot())
        stand.start()
        report = stand.probe_report()
        self.assertEqual(report["отправлено_полос"], 0)
        self.assertEqual(report["вердикт"], "недостаточно данных")

    def test_probe_report_after_answering(self):
        stand = make_stand(FakeBot())
        stand.start()
        feed(stand, full_session())
        stand.journal.append(
            {"at": 0.0, "kind": "sent", "text": "", "payload": fac.SLIDER_DONE, "armed": True}
        )
        report = stand.probe_report()
        self.assertEqual(report["отправлено_полос"], 1)
        self.assertEqual(report["вердикт"], "сервер принял ответ и продолжил работу")

    def test_stop_clears_queue(self):
        bot = FakeBot()
        stand = make_stand(bot, armed=True, timing=fac.TIMING_NATURAL)
        stand.start()
        feed(stand, [(fac.MINIGAME_INTERFACE, SLIDER_A)])
        self.assertTrue(stand.pending)
        stand.stop("тест")
        self.assertEqual(stand.pending, [])
        self.assertEqual(stand.state, fac.S_OFF)


class StateMachineTests(unittest.TestCase):
    def test_off_state_does_nothing(self):
        stand = make_stand(FakeBot())
        self.assertEqual(stand.step(), "стенд выключен")

    def test_walks_locker_then_material(self):
        moves = []

        def travel(b, target):
            moves.append(target)
            b.api.pos = target

        bot = FakeBot(pos=fac.LOCKER)
        stand = make_stand(bot, travel=travel)
        at_locker(stand)          # диалог пройден, стоим на раздевалке
        stand.step()              # взаимодействие с раздевалкой
        self.assertEqual(stand.state, fac.S_MATERIAL)
        stand.step()              # идём к сырью
        self.assertEqual(moves[-1], fac.MATERIAL)
        stand.step()              # на сырье -> взаимодействие
        self.assertEqual(stand.state, fac.S_MACHINE)

    def test_machine_advances_to_belt_after_quiet(self):
        clock = FakeClock()
        real = fac.time
        fac.time = clock
        try:
            stand = make_stand(FakeBot(pos=fac.LOCKER), travel=_walker)
            at_locker(stand)
            step_until(stand, lambda: stand.state == fac.S_MACHINE)
            self.assertEqual(stand.state, fac.S_MACHINE)
            stand.sliders = 1
            stand.last_slider_at = clock.now
            clock.advance(30.0)
            text = stand.step()
            self.assertEqual(stand.state, fac.S_BELT)
            self.assertIn("ленту", text)
        finally:
            fac.time = real

    def test_goal_is_recorded(self):
        stand = make_stand(FakeBot())
        stand.start()
        feed(stand, [(fac.GOAL_INTERFACE, GOAL)])
        self.assertEqual(stand.goal, "Переоденьтесь в рабочую форму")

    def test_cycle_counter(self):
        stand = make_stand(FakeBot())
        stand.start()
        feed(stand, [(fac.NOTIFY_INTERFACE, GOT_ORE)])
        self.assertEqual(stand.cycles, 1)


class DescriptionTests(unittest.TestCase):
    def test_frames_are_described(self):
        self.assertIn("диалог", fac.describe_frame(fac.DIALOG_INTERFACE, DIALOG_OPEN))
        self.assertIn("закрытие", fac.describe_frame(fac.DIALOG_INTERFACE, DIALOG_CLOSE))
        self.assertIn("g=1", fac.describe_frame(fac.MINIGAME_INTERFACE, SLIDER_A))
        self.assertIn("Металлолом", fac.describe_frame(fac.NOTIFY_INTERFACE, GOT_ORE))
        self.assertIn("цель", fac.describe_frame(fac.GOAL_INTERFACE, GOAL))

    def test_describe_survives_garbage(self):
        self.assertIsInstance(fac.describe_frame(999, None), str)
        self.assertIsInstance(fac.describe_frame(999, {"a": 1}), str)


class ListenerTests(unittest.TestCase):
    def test_accepts_emitter_signature(self):
        stand = make_stand(FakeBot())
        stand.start()
        stand.on_receive_json(object(), fac.DIALOG_INTERFACE, DIALOG_OPEN)
        self.assertEqual(stand.dialog_pages, 1)

    def test_accepts_direct_signature(self):
        stand = make_stand(FakeBot())
        stand.start()
        stand.on_interface(fac.DIALOG_INTERFACE, DIALOG_OPEN)
        self.assertEqual(stand.dialog_pages, 1)

    def test_ignores_non_dict(self):
        stand = make_stand(FakeBot())
        stand.start()
        stand.on_interface(fac.MINIGAME_INTERFACE, "чепуха")
        self.assertEqual(stand.sliders, 0)

    def test_ignores_bad_interface_id(self):
        stand = make_stand(FakeBot())
        stand.start()
        stand.on_interface("не число", DIALOG_OPEN)
        self.assertEqual(stand.dialog_pages, 0)


class LiveBytesTests(unittest.TestCase):
    """Наши пакеты против байтов с провода.

    Тела сняты из _diag/factory.pcap. Если билдер поедет, эти тесты
    поймают это раньше, чем сервер.
    """

    LIVE_436 = {
        2048: "0bb4f10000400000",  # раздевалка
        2049: "0bb4f10008400000",  # сырьё
        2: "0bb4f10010000000",     # вход
    }

    def test_interact_bodies_match_capture(self):
        from samp.packets import PacketBuilder

        for obj_id, live in self.LIVE_436.items():
            out = PacketBuilder.rpc_build("picked_up_pickup", obj_id)
            body = out[0] if isinstance(out, tuple) else out
            self.assertEqual(body.hex(), live, f"объект {obj_id}")

    def test_pickup_body_matches_capture(self):
        # Тело поднятия предмета, снято из craft.pcap и pickup.pcap.
        from samp.packets import PacketBuilder

        out = PacketBuilder.rpc_build("picked_up_interact", 18)
        body = out[0] if isinstance(out, tuple) else out
        self.assertEqual(body.hex(), "0b9df10090000000")

    def test_station_object_ids_are_the_live_ones(self):
        # Ровно те id, что уходили в 436 в захвате.
        self.assertEqual(sorted(self.LIVE_436), [2, 2048, 2049])
        self.assertIn(fac.OBJ_LOCKER, self.LIVE_436)
        self.assertIn(fac.OBJ_MATERIAL, self.LIVE_436)
        self.assertIn(fac.OBJ_ENTRY, self.LIVE_436)


if __name__ == "__main__":
    unittest.main(verbosity=2)
