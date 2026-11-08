"""Реплей настоящего захвата через бота шахты.

Ни живого аккаунта, ни сокета: берём кадры из `_diag/mine/mine.pcap`,
прогоняем в том порядке, в каком они пришли, и смотрим, узнаёт ли бот
сценарий. Это проверка на реальных данных, а не на выдуманных.

Запуск:  python3 -m pytest test_mine_replay.py -q -s
"""

import os
import sys

import pytest

import logic.mine as M
from logic.mine import MineBot

ROOT = os.path.dirname(os.path.abspath(__file__))
PCAP = os.path.join(ROOT, "_diag", "mine", "mine.pcap")


class Rec:
    """Собирает всё, что бот увидел и захотел отправить."""

    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(text)


class NoSession:
    connected = False


class NoApi:
    @staticmethod
    def getbotposition():
        return (0.0, 0.0, 0.0)


class ReplayBot:
    api = NoApi()
    session = NoSession()


def read_frames():
    """Кадры интерфейсов из pcap в порядке времени: [(ts, iid, data), ...]."""
    if not os.path.exists(PCAP):
        pytest.skip("нет записи _diag/mine/mine.pcap")
    sys.path.insert(0, os.path.join(ROOT, "_diag"))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pd", os.path.join(ROOT, "_diag", "pcap_decrypt.py")
    )
    pd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pd)

    from core.protocol import RakNetProtocol
    from samp.interfaces import parse_json_payload

    proto = RakNetProtocol(None)
    proto._apply_delivery_order = lambda pkt: [pkt]
    proto._is_duplicate_reliable_packet = lambda pkt: False

    frames = []
    for ts, src, dst, sport, dport, payload in pd.read_pcap(PCAP):
        if dport != 5125 and sport != 5125:
            continue
        try:
            got = proto.parse_datagram(payload)
        except Exception:
            continue
        for p in got:
            d = p.get("data")
            if not d or d[0] != 0x1B:
                continue
            body = d[1:]
            if len(body) < 6:
                continue
            iid = int.from_bytes(body[0:2], "little")
            # parse_json_payload ждёт тело БЕЗ байта типа пакета и отдаёт
            # обёртку; сам JSON лежит в ключе "json".
            parsed = parse_json_payload(body)
            if isinstance(parsed, dict) and isinstance(parsed.get("json"), dict):
                frames.append((ts, iid, parsed["json"]))
    return frames


def test_replay_recognises_the_whole_scenario():
    frames = read_frames()
    assert frames, "в записи не нашлось ни одного кадра интерфейса"

    logs = Rec()
    game = MineBot(ReplayBot(), armed=False, log=logs)
    game.state = M.S_ROUTE

    for ts, iid, data in frames:
        game.on_interface(iid, data)

    # --- что бот обязан был узнать на настоящих кадрах ---
    assert game.dialog_pages >= 3, f"страниц диалога мало: {game.dialog_pages}"
    assert game.sliders >= 1, "ползунок не распознан"
    assert game.captcha_seen >= 1, "капча не распознана"
    assert game.goal, "цель задания не прочитана"
    assert any("Сдайте руду" in n or "руду" in n.lower() for n in game.notifications) \
        or "руду" in str(game.goal).lower(), "сдача груза в кадрах не найдена"

    # В холостом ходе ни одного пакета наружу.
    assert game.pending == []


def test_replay_shows_what_the_bot_would_send():
    """Печатает сценарий — глазами проверить, что шаги те самые."""
    frames = read_frames()
    logs = Rec()
    game = MineBot(ReplayBot(), armed=False, log=logs)
    for ts, iid, data in frames:
        game.on_interface(iid, data)

    wanted = [line for line in logs.lines if "ХОЛОСТОЙ ХОД" in line]
    assert wanted, "бот не захотел отправить вообще ничего"
    print(f"\nкадров интерфейса: {len(frames)}")
    print(f"ответов, которые бот намеревался отправить: {len(wanted)}")
    for line in wanted[:12]:
        print("   ", line[:110])
    print("\nитог:", game.status())


def test_replay_never_answers_captcha_without_solver():
    """В записи капча есть, решателя нет — бот не должен ничего слать."""
    frames = read_frames()
    game = MineBot(ReplayBot(), armed=True, captcha_solver=None)
    for ts, iid, data in frames:
        game.on_interface(iid, data)
    captcha_answers = [
        e for e in game.journal
        if e["kind"] == "queued" and e["payload"] == {"t": 0, "s": ""}
    ]
    assert not captcha_answers
    assert any(e["kind"] == "captcha_no_solver" for e in game.journal)
