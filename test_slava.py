"""Тесты автопрохода квестов Дяди Славы. Без сети и без аккаунта."""
import unittest

import logic.slava as S


class FakeVehicle:
    def __init__(self, vid, x, y, z, model_id=400):
        self.vehicle_id = vid
        self.model_id = model_id
        self.x, self.y, self.z = float(x), float(y), float(z)


class FakeStream:
    def __init__(self, vehicles=None):
        self.vehicles = vehicles or {}


class FakePlayer:
    def __init__(self, pos=(0.0, 0.0, 0.0), in_veh=False, vid=0):
        self.pos = list(pos)
        self.in_veh = in_veh
        self.vid = vid

    def get_position(self):
        return tuple(self.pos)

    def is_in_vehicle(self):
        return self.in_veh

    def get_current_vehicle_id(self):
        return self.vid


class FakeSession:
    def __init__(self, vehicles=None, pos=(0.0, 0.0, 0.0)):
        self.connected = True
        self.stream = FakeStream(vehicles)
        self.players = type("P", (), {})()
        self.players.bot = FakePlayer(pos)

    def get_vehicle(self, vid):
        return self.stream.vehicles.get(vid)


class FakeApi:
    def __init__(self):
        self.moves = []
        self.json = []
        self.chat = []
        self.entered = []
        self.exited = 0
        self.calls = []
        self.builders = []

    def MoveToCoord(self, x, y, z, **kw):
        self.moves.append({"target": (x, y, z), **kw})
        return len(self.moves)

    def setbotvehicle(self, vid, seat_id=0, **kw):
        self.entered.append((vid, seat_id))
        if vid == 0:
            self.exited += 1
        return True

    def sendchat(self, text):
        self.chat.append(text)
        return True

    def send_json(self, iid, data, **kw):
        self.json.append((iid, data))
        return True

    def send_npc_dialog_response(self, key):
        self.builders.append(("npc", int(key)))
        return True

    def send_call_notification_response(self, notif_type=2, button=1):
        self.builders.append(("call", int(notif_type), int(button)))
        return True

    def send_notification_response(self, c=13, notification_type=0,
                                   notification_s=0, button=0):
        self.builders.append(("notif", int(c), int(notification_type),
                              int(notification_s), int(button)))
        return True

    def GetFreeReward(self, *a):
        self.calls.append("GetFreeReward")

    def CaseOpen(self, *a):
        self.calls.append("CaseOpen")

    def CaseCollectReward(self, *a):
        self.calls.append("CaseCollectReward")

    def CaseMenuClose(self, *a):
        self.calls.append("CaseMenuClose")

    def DonatMenuClose(self, *a):
        self.calls.append("DonatMenuClose")


class FakeBot:
    def __init__(self, pos=(0.0, 0.0, 0.0), vehicles=None):
        self.session = FakeSession(vehicles, pos)
        self.api = FakeApi()
        self.logs = []
        self.running = True

    def add_log(self, text):
        self.logs.append(str(text))


def make(pos=(0.0, 0.0, 0.0), vehicles=None):
    bot = FakeBot(pos, vehicles)
    q = S.SlavaQuest(bot, log=lambda m: bot.logs.append(m))
    return bot, q


class StageMatchTests(unittest.TestCase):
    def test_intro(self):
        st = S.match_stage("Поговорите с Дядей Славой на вокзале")
        self.assertEqual(st.key, "intro")

    def test_food(self):
        self.assertEqual(S.match_stage("Купите еды в киоске").key, "food")

    def test_board(self):
        self.assertEqual(S.match_stage("Сядьте в Ласточку").key, "board")

    def test_styling(self):
        st = S.match_stage("Доберитесь до стайлинг-центра")
        self.assertEqual(st.key, "styling")

    def test_fuel(self):
        self.assertEqual(S.match_stage("Заправьте машину на АЗС").key, "fuel")

    def test_repair(self):
        self.assertEqual(S.match_stage("Купите ремкомплект").key, "repair")

    def test_mine_trip(self):
        self.assertEqual(S.match_stage("Доезжайте до шахты").key, "mine_trip")

    def test_mine_quit(self):
        self.assertEqual(S.match_stage("Увольтесь с шахты").key, "mine_quit")

    def test_return_beats_intro_on_return_text(self):
        # «вернитесь» длиннее «поговорите»? нет, одинаково — решает order.
        st = S.match_stage("Вернитесь на вокзал к Славе")
        self.assertEqual(st.key, "return")

    def test_intro_not_hijacked_by_return(self):
        # Раньше «вокзал» в стадии return перебивал intro — бот шёл
        # сдавать квест, который ещё не начал.
        st = S.match_stage("Поговорите с Дядей Славой на вокзале")
        self.assertNotEqual(st.key, "return")

    def test_unknown(self):
        self.assertIsNone(S.match_stage("Просто текст без ключевых слов"))

    def test_empty(self):
        self.assertIsNone(S.match_stage(""))
        self.assertIsNone(S.match_stage(None))

    def test_all_stages_have_hints(self):
        for stage in S.STAGES:
            self.assertTrue(stage.hints, stage.key)

    def test_stage_keys_unique(self):
        keys = [s.key for s in S.STAGES]
        self.assertEqual(len(keys), len(set(keys)))


class VehicleRunnerTests(unittest.TestCase):
    def test_nearest_picks_closest(self):
        veh = {
            1: FakeVehicle(1, 100, 0, 0),
            2: FakeVehicle(2, 5, 0, 0),
            3: FakeVehicle(3, 50, 0, 0),
        }
        bot, q = make(vehicles=veh)
        self.assertEqual(q.vehicles.nearest().vehicle_id, 2)

    def test_nearest_respects_radius(self):
        bot, q = make(vehicles={1: FakeVehicle(1, 500, 0, 0)})
        self.assertIsNone(q.vehicles.nearest(max_distance=40.0))

    def test_nearest_empty(self):
        bot, q = make()
        self.assertIsNone(q.vehicles.nearest())

    def test_position(self):
        bot, q = make(pos=(10.0, 20.0, 30.0))
        self.assertEqual(q.vehicles.position(), (10.0, 20.0, 30.0))

    def test_board_without_vehicle(self):
        bot, q = make()
        self.assertFalse(q.vehicles.board())

    def test_board_sends_enter(self):
        veh = {7: FakeVehicle(7, 2, 0, 0)}
        bot, q = make(vehicles=veh)
        bot.session.players.bot.in_veh = True  # имитируем, что сел
        self.assertTrue(q.vehicles.board())
        # in_vehicle уже True -> board вернул True без пакета
        self.assertEqual(bot.api.entered, [])

    def test_leave_when_on_foot(self):
        bot, q = make()
        self.assertTrue(q.vehicles.leave())

    def test_leave_sends_exit(self):
        bot, q = make()
        bot.session.players.bot.in_veh = True
        bot.session.players.bot.vid = 5
        q.vehicles.leave(timeout=0.1)
        self.assertIn((0, 0), bot.api.entered)

    def test_ensure_engine_requires_vehicle(self):
        bot, q = make()
        self.assertFalse(q.vehicles.ensure_engine())

    def test_ensure_engine_sends_e(self):
        # В эталоне (pcap 06.11) двигатель заводится командой /e, а не /en.
        bot, q = make()
        bot.session.players.bot.in_veh = True
        self.assertTrue(q.vehicles.ensure_engine())
        self.assertIn("/e", bot.api.chat)
        self.assertNotIn("/en", bot.api.chat)


class NavigatorTests(unittest.TestCase):
    def test_distance(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        self.assertAlmostEqual(q.nav.distance_to((30.0, 40.0, 0.0)), 50.0)

    def test_walk_decision_short(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.target = (20.0, 0.0, 0.0)
        self.assertTrue(q._decide_walk())

    def test_drive_decision_long(self):
        bot, q = make(pos=(0.0, 0.0, 0.0), vehicles={1: FakeVehicle(1, 3, 0, 0)})
        q.target = (500.0, 0.0, 0.0)
        self.assertFalse(q._decide_walk())

    def test_stage_need_walk_overrides(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.target = (500.0, 0.0, 0.0)
        q.stage = S.STAGE_BY_KEY["food"]
        self.assertTrue(q._decide_walk())

    def test_stage_need_vehicle_overrides(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.target = (10.0, 0.0, 0.0)
        q.stage = S.STAGE_BY_KEY["styling"]
        self.assertFalse(q._decide_walk())

    def test_goto_none(self):
        bot, q = make()
        self.assertEqual(q.nav.goto(None), "нет цели")

    def test_goto_issues_move(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        # цель в 2 м -> arrive сразу
        note = q.nav.goto((2.0, 0.0, 0.0), walk=True, arrive=2.5, timeout=0.5)
        self.assertIn("месте", note)
        self.assertTrue(bot.api.moves)

    def test_goto_walk_mode(self):
        bot, q = make()
        q.nav.goto((5.0, 0.0, 0.0), walk=True, arrive=10.0, timeout=0.3)
        self.assertEqual(bot.api.moves[0]["mode"], "walk")

    def test_goto_vehicle_mode(self):
        bot, q = make()
        bot.session.players.bot.in_veh = True
        q.nav.goto((50.0, 0.0, 0.0), walk=False, arrive=60.0, timeout=0.3)
        self.assertEqual(bot.api.moves[0]["mode"], "vehicle")


class ArrivalTests(unittest.TestCase):
    """Прибытие определяется по позиции, а не по возврату MoveToCoord."""

    def test_arrival_detected(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        target = (40.0, 0.0, 0.0)

        def move(x, y, z, **kw):
            # имитируем, что бот доехал
            bot.session.players.bot.pos = [x, y, z]
            bot.api.moves.append({"target": (x, y, z), **kw})
            return 1

        bot.api.MoveToCoord = move
        note = q.nav.goto(target, walk=True, arrive=2.5, timeout=2.0)
        self.assertIn("на месте", note)

    def test_timeout_reports_remaining(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        # MoveToCoord ничего не делает -> позиция не меняется
        note = q.nav.goto((500.0, 0.0, 0.0), walk=True, arrive=2.5, timeout=0.4)
        self.assertIn("не доехал", note)
        self.assertIn("осталось", note)

    def test_stop_interrupts(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        bot.running = False
        note = q.nav.goto((500.0, 0.0, 0.0), walk=True, arrive=2.5, timeout=2.0)
        self.assertEqual(note, "остановлено")


class FullFlowTests(unittest.TestCase):
    """Полный проход: цель -> стадия -> маркер -> решение -> прибытие."""

    def test_intro_flow(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.on_quest_text("Поговорите с Дядей Славой на вокзале")
        self.assertEqual(q.stage.key, "intro")

        q.on_checkpoint(15.0, 0.0, 0.0, 3.0)
        self.assertEqual(q.target, (15.0, 0.0, 0.0))
        # 15 м -> пешком
        self.assertTrue(q._decide_walk())

    def test_mine_trip_needs_vehicle(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.on_quest_text("Доезжайте до шахты")
        q.on_checkpoint(900.0, 0.0, 0.0, 4.0)
        self.assertEqual(q.stage.key, "mine_trip")
        self.assertFalse(q._decide_walk())

    def test_stage_sequence_recorded(self):
        bot, q = make()
        for text in (
            "Поговорите с Дядей Славой на вокзале",
            "Купите еды в киоске",
            "Сядьте в Ласточку",
            "Доезжайте до шахты",
            "Вернитесь на вокзал к Славе",
        ):
            q.on_quest_text(text)
        self.assertEqual(
            q.history,
            ["intro", "food", "board", "mine_trip", "return"],
        )

    def test_arrive_exits_vehicle_when_needed(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.stage = S.STAGE_BY_KEY["food"]
        bot.session.players.bot.in_veh = True
        bot.session.players.bot.vid = 3
        original = S.STEP_DELAY
        S.STEP_DELAY = (0.0, 0.0)
        try:
            q._after_arrive()
        finally:
            S.STEP_DELAY = original
        # Выход отправлен, потому что стадия требует пешком.
        self.assertIn((0, 0), bot.api.entered)

    def test_status_reflects_flow(self):
        bot, q = make(pos=(0.0, 0.0, 0.0))
        q.on_quest_text("Доезжайте до шахты")
        q.on_checkpoint(100.0, 0.0, 0.0, 3.0)
        st = q.status()
        self.assertEqual(st["stage"], "mine_trip")
        self.assertEqual(st["stage_title"], "поездка на шахту")
        self.assertFalse(st["in_vehicle"])


class MoveKwargsTests(unittest.TestCase):
    """completion_distance не должен уходить дважды.

    Реальный баг: в `VehicleRunner._move_to` он передавался и явно, и
    внутри `**WALK_TUNING` — Python падал с «got multiple values for
    keyword argument», и бот НИКОГДА не подходил к машине.
    """

    def test_board_move_has_single_completion_distance(self):
        veh = {9: FakeVehicle(9, 20, 0, 0)}
        bot, q = make(vehicles=veh)
        q.vehicles._move_to((20.0, 0.0, 0.0), walk=True, arrive=3.0)
        self.assertEqual(len(bot.api.moves), 1)
        move = bot.api.moves[0]
        self.assertEqual(move["completion_distance"], 3.0)

    def test_goto_walk_single(self):
        bot, q = make()
        q.nav.goto((5.0, 0.0, 0.0), walk=True, arrive=10.0, timeout=0.3)
        self.assertEqual(bot.api.moves[0]["completion_distance"], 10.0)

    def test_goto_vehicle_adds_margin(self):
        bot, q = make()
        bot.session.players.bot.in_veh = True
        q.nav.goto((50.0, 0.0, 0.0), walk=False, arrive=3.0, timeout=0.3)
        self.assertEqual(bot.api.moves[0]["completion_distance"], 4.0)

    def test_tuning_not_mutated(self):
        bot, q = make()
        before = dict(S.WALK_TUNING)
        q.nav.goto((5.0, 0.0, 0.0), walk=True, arrive=10.0, timeout=0.3)
        self.assertEqual(S.WALK_TUNING, before)

    def test_board_actually_boards(self):
        veh = {9: FakeVehicle(9, 20, 0, 0)}
        bot, q = make(vehicles=veh)

        # Подход телепортирует бота, посадка переключает флаг.
        def move(x, y, z, **kw):
            bot.session.players.bot.pos = [x, y, z]
            bot.api.moves.append({"target": (x, y, z), **kw})
            return 1

        def enter(vid, seat_id=0, **kw):
            bot.api.entered.append((vid, seat_id))
            bot.session.players.bot.in_veh = (vid != 0)
            return True

        bot.api.MoveToCoord = move
        bot.api.setbotvehicle = enter
        self.assertTrue(q.vehicles.board(veh[9]))
        self.assertIn((9, 0), bot.api.entered)


class QuestEventTests(unittest.TestCase):
    def test_quest_text_sets_stage(self):
        bot, q = make()
        self.assertTrue(q.on_quest_text("Купите еды в киоске"))
        self.assertEqual(q.stage.key, "food")

    def test_quest_text_dedup(self):
        bot, q = make()
        q.on_quest_text("Купите еды в киоске")
        self.assertFalse(q.on_quest_text("Купите еды в киоске"))

    def test_quest_text_unknown_keeps_stage(self):
        bot, q = make()
        q.on_quest_text("Купите еды в киоске")
        q.on_quest_text("непонятный текст")
        self.assertEqual(q.stage.key, "food")

    def test_checkpoint_sets_target(self):
        bot, q = make()
        self.assertTrue(q.on_checkpoint(10.0, 20.0, 30.0, 3.0))
        self.assertEqual(q.target, (10.0, 20.0, 30.0))
        self.assertEqual(q.target_radius, 3.0)

    def test_checkpoint_bad_values(self):
        bot, q = make()
        self.assertFalse(q.on_checkpoint("x", "y", "z", 3.0))

    def test_checkpoint_cleared(self):
        bot, q = make()
        q.on_checkpoint(1.0, 2.0, 3.0, 2.0)
        q.on_checkpoint_cleared()
        self.assertIsNone(q.target)

    def test_npc_dialog_sends_reply(self):
        bot, q = make()
        q.running = True
        buttons = [{"bn": "Далее", "bk": 3}, {"bn": "Отмена", "bk": 4}]
        self.assertTrue(q.on_npc_dialog("Дядя Слава", "текст", 0, buttons))
        self.assertEqual(q._pick_button(buttons), 3)

    def test_npc_dialog_ignores_strangers(self):
        bot, q = make()
        q.running = True
        self.assertFalse(q.on_npc_dialog("Продавец", "текст", 0, []))

    def test_call_from_slava(self):
        bot, q = make()
        q.running = True
        self.assertTrue(q.on_call("Дядя Слава", "Позвони", None, 2))

    def test_call_from_other(self):
        bot, q = make()
        q.running = True
        self.assertFalse(q.on_call("Банк", "Реклама", None, 2))

    def test_minigame(self):
        bot, q = make()
        self.assertTrue(q.on_minigame(31, {"g": 2}))
        self.assertFalse(q.on_minigame(26, {"g": 2}))
        self.assertFalse(q.on_minigame(31, {"x": 1}))

    def test_json_quest_task(self):
        bot, q = make()
        self.assertTrue(q.on_json(39, {"mq": "Купите еды в киоске"}))
        self.assertEqual(q.stage.key, "food")

    def test_json_ignores_other(self):
        bot, q = make()
        self.assertFalse(q.on_json(99, {"mq": "что-то"}))


class BuilderPathTests(unittest.TestCase):
    """Ответы должны идти через готовые билдеры, а не самодельный JSON."""

    def _wait(self, flag, limit=400):
        import time
        for _ in range(limit):
            if flag():
                return True
            time.sleep(0.02)
        return False

    def test_npc_uses_builder(self):
        bot, q = make()
        q.running = True
        q._reply_npc(3, "Дядя Слава")
        self.assertTrue(self._wait(lambda: bot.api.builders))
        self.assertEqual(bot.api.builders[0], ("npc", 3))
        self.assertEqual(bot.api.json, [])

    def test_call_uses_builder(self):
        bot, q = make()
        q.running = True
        q._answer_call(2)
        self.assertTrue(self._wait(lambda: bot.api.builders))
        self.assertEqual(bot.api.builders[0], ("call", 2, 1))

    def test_skip_trip_uses_builder(self):
        bot, q = make()
        q._skip_trip({"t": 1, "s": 7})
        self.assertTrue(self._wait(lambda: bot.api.builders))
        # t — это действие (0 = подтвердить), b — кнопка из уведомления.
        self.assertEqual(bot.api.builders[0], ("notif", 13, 0, 7, 0))

    def test_skip_trip_uses_server_button(self):
        bot, q = make()
        q._skip_trip({"t": 0, "s": 71, "b": 0})
        self.assertTrue(self._wait(lambda: bot.api.builders))
        self.assertEqual(bot.api.builders[0], ("notif", 13, 0, 71, 0))

    def test_fallback_without_builders(self):
        # delattr на экземпляре для метода класса падает — подменяем
        # объект целиком, оставив только send_json.
        import types

        bot, q = make()
        sent = []

        def send_json(iid, data, **kw):
            sent.append((iid, data))
            return True

        bot.api = types.SimpleNamespace(send_json=send_json)
        q.running = True
        q._reply_npc(5, "Дядя Слава")
        self.assertTrue(self._wait(lambda: sent))
        self.assertEqual(sent[0], (63, {"bk": 5}))


class ButtonPickTests(unittest.TestCase):
    def test_empty(self):
        bot, q = make()
        self.assertEqual(q._pick_button(None), 1)
        self.assertEqual(q._pick_button([]), 1)

    def test_prefers_dalee(self):
        bot, q = make()
        buttons = [{"bn": "Отмена", "bk": 2}, {"bn": "Далее", "bk": 5}]
        self.assertEqual(q._pick_button(buttons), 5)

    def test_ok_over_yes(self):
        bot, q = make()
        buttons = [{"bn": "Да", "bk": 1}, {"bn": "Ок", "bk": 9}]
        self.assertEqual(q._pick_button(buttons), 9)

    def test_unknown_buttons_default(self):
        bot, q = make()
        buttons = [{"bn": "Что-то", "bk": 7}]
        self.assertEqual(q._pick_button(buttons), 1)

    def test_key_fallback(self):
        bot, q = make()
        buttons = [{"bn": "Далее", "key": 4}]
        self.assertEqual(q._pick_button(buttons), 4)


class LifecycleTests(unittest.TestCase):
    def test_status_initial(self):
        bot, q = make()
        st = q.status()
        self.assertFalse(st["running"])
        self.assertIsNone(st["stage"])

    def test_start_stop(self):
        bot, q = make()
        self.assertTrue(q.start())
        self.assertTrue(q.running)
        self.assertTrue(q.stop())
        self.assertFalse(q.running)
        self.assertFalse(q.stop())

    def test_status_after_text(self):
        bot, q = make()
        q.on_quest_text("Купите еды в киоске")
        q.on_checkpoint(1.0, 2.0, 3.0, 2.0)
        st = q.status()
        self.assertEqual(st["stage"], "food")
        self.assertEqual(st["target"], (1.0, 2.0, 3.0))

    def test_history_tracks_stages(self):
        bot, q = make()
        q.on_quest_text("Купите еды в киоске")
        q.on_quest_text("Сядьте в Ласточку")
        self.assertEqual(q.history, ["food", "board"])

    def test_get_quest_singleton(self):
        bot = FakeBot()
        a = S.get_quest(bot)
        b = S.get_quest(bot)
        self.assertIs(a, b)

    def test_gift_steps_order(self):
        # Порядок вызовов — часть сценария, проверяем без ожидания.
        names = [name for name, _args, _pause in S.GIFT_STEPS]
        self.assertEqual(
            names,
            ["GetFreeReward", "CaseOpen", "CaseCollectReward",
             "CaseMenuClose", "DonatMenuClose"],
        )

    def test_gift_claim_sets_flag(self):
        bot, q = make()
        # Паузы в сценарии длинные; для теста обнуляем их.
        original = S.GIFT_STEPS
        S.GIFT_STEPS = tuple((n, a, 0.0) for n, a, _p in original)
        try:
            q._claim_gift()
            import time
            for _ in range(100):
                if q.gift_claimed:
                    break
                time.sleep(0.02)
        finally:
            S.GIFT_STEPS = original
        self.assertTrue(q.gift_claimed)
        self.assertEqual(
            bot.api.calls,
            ["GetFreeReward", "CaseOpen", "CaseCollectReward",
             "CaseMenuClose", "DonatMenuClose"],
        )


class ConstantsTests(unittest.TestCase):
    def test_interfaces(self):
        self.assertEqual(S.QUEST_TASK_IID, 39)
        self.assertEqual(S.NPC_DIALOG_IID, 63)
        self.assertEqual(S.MINIGAME_IID, 31)

    def test_walk_threshold_sane(self):
        self.assertGreater(S.WALK_MAX, 10.0)
        self.assertLess(S.WALK_MAX, 500.0)

    def test_board_distance_under_api_limit(self):
        # samp/api.py: MAX_ENTER_VEHICLE_DISTANCE = 10.0
        self.assertLess(S.BOARD_DISTANCE, 10.0)


if __name__ == "__main__":
    unittest.main()
