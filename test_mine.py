"""Тесты бота шахты. Ничего живого: ни сессии, ни аккаунта, ни сокета."""

import time

import pytest

import logic.mine as M
from logic.mine import MineBot


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, packet, **kw):
        self.sent.append((packet, kw))
        return True


class FakeSession:
    def __init__(self, connected=True):
        self.connected = connected
        self.json = []
        self._sender = FakeSender()

    def send_json(self, iid, data, **kw):
        if not self.connected:
            return False
        self.json.append((iid, data))
        return True


class FakeApi:
    def __init__(self, pos=(0.0, 0.0, 0.0)):
        self.pos = pos
        self.moves = []

    def getbotposition(self):
        return self.pos

    def MoveToCoord(self, x, y, z, **kw):
        self.moves.append((x, y, z, kw))
        self.pos = (x, y, z)


class FakeBot:
    def __init__(self, pos=(0.0, 0.0, 0.0), connected=True):
        self.api = FakeApi(pos)
        self.session = FakeSession(connected)


def make(**kw):
    """Стенд по умолчанию уже пережил отстой после спавна.

    Иначе каждый тест начинался бы с тридцатисекундного ожидания, и было
    бы не видно, что тест проверяет на самом деле. Отстой проверяется
    отдельно, своими тестами.
    """
    bot = kw.pop("bot", None) or FakeBot()
    logs = []
    game = MineBot(bot, log=logs.append, **kw)
    game.spawn_at = time.time() - (game.settle_seconds + 3600.0)
    return game, bot, logs


# ------------------------------------------------ точки из записи

def test_points_come_from_the_capture():
    """Каждая точка — из конкретного кадра pcap, а не из головы."""
    assert M.MINE_EXTERIOR == (2380.63, 1657.64, 13.71)
    assert M.INT_LOCKER == (540.62, 515.31, 1000.32)
    assert M.ROUTE_UNLOAD == (557.13, 516.24, 999.71)
    assert M.ROUTE_FACE == (554.04, 542.82, 998.70)


def test_route_is_a_shuttle_not_a_circle():
    """Между лентой и забоем 26 м по Y — это челнок, как в записи."""
    dist = M._dist(M.ROUTE_UNLOAD, M.ROUTE_FACE)
    assert 25.0 < dist < 28.0


def test_is_interior_splits_street_from_inside():
    assert not M.is_interior(M.MINE_EXTERIOR)
    assert not M.is_interior(M.MINE_ENTRY)
    assert M.is_interior(M.INT_SPAWN)
    assert M.is_interior(M.ROUTE_UNLOAD)


def test_object_ids_match_capture():
    assert M.OBJ_CLOSE == 16
    assert M.OBJ_BUTTON_A == 2048
    assert M.OBJ_SLIDER == 2049
    assert M.RPC_INTERACT == 436


# ------------------------------------------------ холостой ход

def test_idle_mode_sends_nothing_whatsoever():
    game, bot, _ = make(armed=False)
    game.start()
    game.on_interface(M.DIALOG_INTERFACE, {"o": 1, "d": "текст", "b": "Далее"})
    game.on_interface(M.MINIGAME_INTERFACE, {"o": 1, "t": 1})
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    for _ in range(5):
        game.step()
    game._flush()
    assert bot.session.json == []
    assert bot.session._sender.sent == []


def test_idle_mode_still_explains_what_it_would_do():
    game, _, logs = make(armed=False)
    game.on_interface(M.DIALOG_INTERFACE, {"o": 1, "d": "стр", "b": "Далее"})
    assert any("ХОЛОСТОЙ ХОД" in line for line in logs)


# ------------------------------------------------ диалог

def test_dialog_page_gets_next_answer():
    game, bot, _ = make(armed=True)
    game.on_interface(M.DIALOG_INTERFACE, {"o": 1, "d": "стр", "b": "Далее"})
    time.sleep(0.08)
    game._flush()
    assert (M.DIALOG_INTERFACE, M.DIALOG_NEXT) in bot.session.json


def test_dialog_close_is_counted_and_moves_on():
    game, bot, _ = make(armed=True)
    game.dialog_pages = 3
    game.on_interface(M.DIALOG_INTERFACE, {"c": 1})
    assert game.dialog_closed == 1
    game.state = M.S_DIALOG
    assert "переодеваться" in game.step()


# ------------------------------------------------ ползунок

# Кадры сервера ровно как в записи (интерфейс 31).
FRAME_SLIDER = {
    "g": 1, "o": 1, "p_max": 50, "p_max_bar": 100, "p_start": 0,
    "p_tick": 1, "p_timer": 180, "p_title": "Раскопка руды", "x": 1,
}
FRAME_HIT = {"o": 1, "g": 2}


def _flush_queued(game):
    """Сдвинуть очередь в прошлое и отправить — не ждём 4 секунды."""
    game.pending = [(0.0,) + tuple(item[1:]) for item in game.pending]
    game._flush()


def test_slider_phase_answers_with_t1_on_the_formula():
    game, bot, _ = make(armed=True)
    game.on_interface(M.MINIGAME_INTERFACE, FRAME_SLIDER)
    assert game.sliders == 1
    # В очереди лежит именно ответ на полосу, и с задержкой по формуле.
    assert len(game.pending) == 1
    when, kind, iid, payload, _note = game.pending[0]
    assert kind == "json" and iid == M.MINIGAME_INTERFACE
    assert payload == M.SLIDER_DONE
    assert when - time.time() > 3.0
    _flush_queued(game)
    assert (M.MINIGAME_INTERFACE, M.SLIDER_DONE) in bot.session.json


def test_hit_phase_answers_with_s1():
    game, bot, _ = make(armed=True)
    game.on_interface(M.MINIGAME_INTERFACE, FRAME_HIT)
    assert game.hits == 1
    _flush_queued(game)
    assert (M.MINIGAME_INTERFACE, M.SLIDER_HIT) in bot.session.json


def test_frame_without_g_is_not_a_minigame():
    """Ключа t сервер не шлёт, а кадр без g — не мини-игра: не отвечаем."""
    game, bot, _ = make(armed=True)
    game.on_interface(M.MINIGAME_INTERFACE, {"o": 1, "t": 1})
    assert game.sliders == 0
    assert game.hits == 0
    assert game.pending == []


def test_slider_steps_are_rpc_436_with_object_2049():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.INT_SLIDER))
    game.state = M.S_SLIDER
    game.started_at = time.time()
    game.step()
    game._flush()
    assert len(bot.session._sender.sent) == 1
    packet, kw = bot.session._sender.sent[0]
    assert kw.get("rpc_name") == "picked_up_pickup"
    # Тело RPC 436 — int32 с номером объекта. Хвост пакета побайтово сверять
    # нельзя: заголовок RPC не выровнен по байту, тело сдвинуто на 4 бита.
    # Поэтому разбираем пакет нашим же парсером и сверяем уже его.
    from samp.parsers import RPC

    parsed = RPC.read_rpc(packet[1:])
    assert parsed["rpc_id"] == M.RPC_INTERACT
    assert parsed["payload"] == (2049).to_bytes(4, "little")


def test_slider_step_cap_is_enforced():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.INT_SLIDER))
    game.state = M.S_SLIDER
    game.started_at = time.time()
    game.slider_steps = M.SLIDER_MAX_STEPS
    assert "кончились" in game.step()
    assert game.state == M.S_SKIP2


# ------------------------------------------------ капча

class Solver:
    def __init__(self, answer="jwzN"):
        self.answer = answer
        self.seen = []

    def solve(self, data):
        self.seen.append(data)
        return self.answer


def test_captcha_answer_goes_to_interface_26():
    solver = Solver("jwzN")
    game, bot, _ = make(armed=True, captcha_solver=solver)
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    time.sleep(0.20)
    game._flush()
    assert (M.CAPTCHA_INTERFACE, {"t": 0, "s": "jwzN"}) in bot.session.json
    assert game.captcha_solved == 1
    # Решатель получает не сырой кадр, а CaptchaChallenge из logic/captcha.py
    challenge = solver.seen[0]
    assert challenge.image_url == "http://x/y.png"
    assert challenge.interface_id == M.CAPTCHA_INTERFACE
    assert challenge.kind == "image_url"


def test_captcha_without_solver_is_not_answered():
    game, bot, _ = make(armed=True)
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    game._flush()
    assert bot.session.json == []
    assert any(e["kind"] == "captcha_no_solver" for e in game.journal)


def test_captcha_without_url_is_skipped():
    game, bot, _ = make(armed=True, captcha_solver=Solver())
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1})
    game._flush()
    assert bot.session.json == []
    assert any(e["kind"] == "captcha_no_url" for e in game.journal)


def test_captcha_solver_crash_is_contained():
    class Boom:
        def solve(self, data):
            raise RuntimeError("нет сети")

    game, bot, _ = make(armed=True, captcha_solver=Boom())
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    game._flush()
    assert bot.session.json == []
    assert "нет сети" in (game.last_error or "")


def test_captcha_empty_answer_is_not_sent():
    game, bot, _ = make(armed=True, captcha_solver=Solver(""))
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    game._flush()
    assert bot.session.json == []


# ------------------------------------------------ автомат

def test_full_chain_reaches_route():
    bot = FakeBot(pos=M.INT_SPAWN)
    game, bot, _ = make(armed=True, bot=bot)
    game.start()
    assert game.state == M.S_TRAVEL

    # уже на шахте
    bot.api.pos = M.MINE_EXTERIOR
    game.step()
    assert game.state == M.S_ENTER

    # прыгнули в интерьер
    bot.api.pos = M.INT_SPAWN
    game.step()
    assert game.state == M.S_DIALOG

    game.on_interface(M.DIALOG_INTERFACE, {"c": 1})
    assert "переодеваться" in game.step()
    assert game.state == M.S_LOCKER


def test_enter_timeout_fails_loudly():
    bot = FakeBot(pos=M.MINE_ENTRY)
    game, bot, _ = make(armed=True, bot=bot)
    game.state = M.S_ENTER
    game.step_at = time.time() - (M.ENTER_TIMEOUT + 1)
    game.step()
    assert game.state == M.S_FAILED


def test_route_bounces_between_two_points():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.ROUTE_FACE))
    game.state = M.S_ROUTE
    game.started_at = time.time()
    game.route_towards_face = True
    # стоим ровно на забое, куда и идём — сразу разворот
    game.step()
    assert game.route_legs == 1
    assert game.route_towards_face is False


def test_route_round_limit_goes_to_unload():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.ROUTE_UNLOAD), route_rounds=1)
    game.state = M.S_ROUTE
    game.started_at = time.time()
    game.route_legs = 2
    assert "сдавать" in game.step()
    assert game.state == M.S_UNLOAD


def test_route_gives_up_after_timeout():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=(0.0, 0.0, 1000.0)))
    game.state = M.S_ROUTE
    game.started_at = time.time() - (M.ROUTE_GIVE_UP + 1)
    game.step()
    assert game.state == M.S_UNLOAD


def test_unload_with_captcha_unsolved_waits():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.ROUTE_UNLOAD))
    game.state = M.S_UNLOAD
    game.captcha_seen = 1
    game.captcha_solved = 0
    assert "капча" in game.step()


def test_unload_after_load_finishes():
    game, bot, _ = make(armed=True, bot=FakeBot(pos=M.ROUTE_UNLOAD))
    game.state = M.S_UNLOAD
    game.loaded = 2
    game.step()
    assert game.state == M.S_DONE


def test_off_state_does_nothing():
    game, bot, _ = make(armed=True)
    game.state = M.S_OFF
    assert "выключен" in game.step()


# ------------------------------------------------ защита

def test_disconnected_session_does_not_lose_packets_silently():
    game, bot, _ = make(armed=True, bot=FakeBot(connected=False))
    game._send_json(M.DIALOG_INTERFACE, M.DIALOG_NEXT, "тест")
    game._flush()
    assert game.last_error == "сессия не подключена"
    assert any(e["kind"] == "error" for e in game.journal)


def test_send_failure_is_recorded():
    class BrokenSession(FakeSession):
        def send_json(self, iid, data, **kw):
            raise OSError("сокет закрыт")

    bot = FakeBot()
    bot.session = BrokenSession()
    game, _, _ = make(armed=True, bot=bot)
    game._send_json(M.DIALOG_INTERFACE, M.DIALOG_NEXT, "тест")
    game._flush()
    assert "сокет закрыт" in (game.last_error or "")


def test_status_has_all_counters():
    game, _, _ = make(armed=False)
    st = game.status()
    for key in ("состояние", "боевой", "ползунков", "капч_решено", "сдано"):
        assert key in st
    assert st["боевой"] is False


def test_journal_is_readable():
    game, _, _ = make(armed=False)
    game.start()
    text = game.dump_journal()
    assert "[ШАХТА]" not in text
    assert "start" in text


def test_no_travel_means_it_says_so_and_stands_still():
    game, bot, logs = make(armed=True, bot=FakeBot(pos=(0.0, 0.0, 0.0)))
    game.state = M.S_TRAVEL
    game.step()
    assert bot.api.moves == []
    assert any("не задан" in line and "транспорт" in line for line in logs)


def test_captcha_frame_becomes_a_real_challenge():
    """Кадр 26 обязан превратиться в CaptchaChallenge, а не уйти словарём."""
    solver = Solver("aaaa")
    game, bot, _ = make(armed=True, captcha_solver=solver)
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "https://cdn/x.png"})
    challenge = solver.seen[0]
    assert not isinstance(challenge, dict)
    assert challenge.image_url == "https://cdn/x.png"


def test_captcha_frame_without_url_yields_no_challenge():
    """26 без картинки — не капча; решатель не должен её видеть."""
    solver = Solver("bbbb")
    game, bot, _ = make(armed=True, captcha_solver=solver)
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1})
    assert game.captcha_solved == 0
    assert any(e["kind"] == "captcha_no_url" for e in game.journal)


# ------------------------------------------------ где топаем, где летим

class Recorder:
    """Перевозчик-пустышка: только записывает, куда позвали."""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def __call__(self, bot, target):
        self.calls.append(tuple(target))


def test_inside_interior_uses_walk_not_teleport():
    """Внутри шахты — шаг. Плечи по 26 м, в записи там 4.3 м/с."""
    outdoor, indoor = Recorder("outdoor"), Recorder("indoor")
    bot = FakeBot(pos=M.ROUTE_UNLOAD)
    game = MineBot(bot, travel=outdoor, travel_indoor=indoor, armed=True)
    game._goto(M.ROUTE_FACE, "забой")
    assert indoor.calls == [M.ROUTE_FACE]
    assert outdoor.calls == []


def test_outside_uses_outdoor_travel():
    outdoor, indoor = Recorder("outdoor"), Recorder("indoor")
    bot = FakeBot(pos=(850.0, 828.0, 13.4))
    game = MineBot(bot, travel=outdoor, travel_indoor=indoor, armed=True)
    game._goto(M.MINE_EXTERIOR, "шахта")
    assert outdoor.calls == [M.MINE_EXTERIOR]
    assert indoor.calls == []


def test_heading_into_the_interior_never_teleports():
    """Цель в интерьере — идём пешком, даже если стоим снаружи.

    Правило жёсткое с умыслом: телепорт внутрь шахты не должен случиться
    ни при каких раскладах. Проверка по обеим точкам это и обеспечивает.
    """
    outdoor, indoor = Recorder("outdoor"), Recorder("indoor")
    bot = FakeBot(pos=M.MINE_EXTERIOR)
    game = MineBot(bot, travel=outdoor, travel_indoor=indoor, armed=True)
    game._goto(M.INT_LOCKER, "переодевание")
    assert indoor.calls == [M.INT_LOCKER]
    assert outdoor.calls == []


def test_indoor_falls_back_to_single_travel():
    """Один перевозчик на всё — законно, интерьерный берётся тот же."""
    only = Recorder("only")
    bot = FakeBot(pos=M.INT_SPAWN)
    game = MineBot(bot, travel=only, armed=True)
    game._goto(M.INT_LOCKER, "переодевание")
    assert only.calls == [M.INT_LOCKER]


def test_spawn_to_locker_is_a_walk_not_a_jump():
    """Полный проход автомата: внутри не должно быть ни одного телепорта."""
    outdoor, indoor = Recorder("outdoor"), Recorder("indoor")
    bot = FakeBot(pos=M.INT_SPAWN)
    game = MineBot(bot, travel=outdoor, travel_indoor=indoor, armed=True)
    game.state = M.S_LOCKER
    game.step()
    assert indoor.calls and not outdoor.calls


# ------------------------------------------------ отстой после спавна

def test_start_waits_before_the_first_action():
    """Сразу после спавна бот молчит тридцать секунд."""
    bot = FakeBot()
    game = MineBot(bot, armed=True, settle_seconds=30.0)
    game.mark_spawn()
    game.start()
    assert game.state == M.S_SETTLE
    assert game.settle_left() > 29.0
    text = game.step()
    assert "осталось" in text
    assert game.state == M.S_SETTLE


def test_nothing_is_sent_during_settle():
    """Главное требование: за отстой наружу не уходит ни один пакет."""
    bot = FakeBot()
    game = MineBot(bot, armed=True, settle_seconds=30.0)
    game.mark_spawn()
    game.start()
    # Сервер уже шлёт диалог, полосу и капчу — а мы молчим.
    game.on_interface(M.DIALOG_INTERFACE, {"o": 1, "d": "стр", "b": "Далее"})
    game.on_interface(M.MINIGAME_INTERFACE, FRAME_SLIDER)
    game.on_interface(M.MINIGAME_INTERFACE, FRAME_HIT)
    game.on_interface(M.CAPTCHA_INTERFACE, {"o": 1, "u": "http://x/y.png"})
    game.step()
    game._flush()
    assert bot.session.json == []
    assert bot.session._sender.sent == []
    held = [e for e in game.journal if e["kind"] == "held"]
    assert held, "отложенные действия не помечены"


def test_teleport_is_refused_during_settle():
    bot = FakeBot(pos=(0.0, 0.0, 14.0))
    moved = []
    game = MineBot(
        bot, armed=True, settle_seconds=30.0,
        travel=lambda b, t: moved.append(t),
    )
    game.mark_spawn()
    game.start()
    game.step()
    assert moved == []


def test_after_settle_everything_works_again():
    bot = FakeBot(pos=(0.0, 0.0, 14.0))
    game = MineBot(bot, armed=True, settle_seconds=0.0)
    game.mark_spawn(time.time() - 31.0)
    game.start()
    assert game.state == M.S_TRAVEL
    game.on_interface(M.DIALOG_INTERFACE, {"o": 1, "d": "стр", "b": "Далее"})
    time.sleep(0.08)
    game._flush()
    assert (M.DIALOG_INTERFACE, M.DIALOG_NEXT) in bot.session.json


def test_settle_ends_and_switches_to_travel():
    bot = FakeBot(pos=(0.0, 0.0, 14.0))
    game = MineBot(bot, armed=True, settle_seconds=30.0)
    game.mark_spawn(time.time() - 31.0)
    game.state = M.S_SETTLE
    game.step()
    assert game.state == M.S_TRAVEL


def test_zero_settle_means_no_waiting():
    """--settle 0 — для отладки: не ждём вовсе."""
    bot = FakeBot()
    game = MineBot(bot, armed=True, settle_seconds=0.0)
    game.start()
    assert game.state == M.S_TRAVEL
    assert game.is_settled()


def test_spawn_time_is_remembered_and_logged():
    game, _, logs = make(armed=True)
    game.mark_spawn(1000.0)
    assert game.spawn_at == 1000.0
    assert any("спавн подтверждён" in line for line in logs)


def test_only_thirty_seconds_not_more():
    """Ровно тридцать, а не «пока не надоест»."""
    bot = FakeBot()
    game = MineBot(bot, armed=True, settle_seconds=30.0)
    game.mark_spawn(time.time() - 29.0)
    assert not game.is_settled()
    assert 0.5 < game.settle_left() < 1.5


# ------------------------------------------------ каждый шаг в лог

def test_every_travel_is_logged_with_coordinates():
    bot = FakeBot(pos=(850.0, 828.0, 13.4))
    game = MineBot(
        bot, armed=False,
        travel=lambda b, t: None,
        travel_indoor=lambda b, t: None,
    )
    game.spawn_at = time.time() - 9999
    game._goto(M.MINE_EXTERIOR, "шахта")
    actions = [e["text"] for e in game.journal if e["kind"] == "action"]
    line = next(t for t in actions if "переход «шахта»" in t)
    # В строке обязаны быть: расстояние, точка «откуда» и точка «куда».
    assert "1741.0 м" in line, line
    assert "850.0,828.0,13.4" in line, line
    assert "2380.6,1657.6,13.7" in line, line


def test_transition_to_mine_is_logged():
    bot = FakeBot(pos=M.MINE_EXTERIOR)
    game = MineBot(bot, armed=False)
    game.spawn_at = time.time() - 9999
    game.state = M.S_TRAVEL
    game.step()
    actions = [e["text"] for e in game.journal if e["kind"] == "action"]
    assert any("на шахте" in t for t in actions), actions
