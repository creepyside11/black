"""Вход в общий мир и реальная статистика.

Проверяется то, из-за чего бот раньше стоял в пустом мире: Spawn уходит
один раз и только с разрешения сервера, а доказательством мира считаются
другие игроки в стриме — не собственный игрок и не NPC.
"""

import time
import unittest
from unittest import mock

from logic import real_world, stats


class FakePlayer:
    def __init__(self, name, x=0.0, y=0.0, z=0.0, npc=False):
        self.name = name
        self.x = x
        self.y = y
        self.z = z
        self.is_npc = npc


class FakePlayers:
    def __init__(self, stream, own_id=1):
        self._stream = stream
        self.bot = type("Own", (), {"player_id": own_id})()

    def get_streamed_players(self):
        return dict(self._stream)


class FakeServerInfo:
    def __init__(self, spawn_response=True):
        self.spawn_response = spawn_response


class FakeSession:
    def __init__(self, stream=None, own_id=1, spawn_response=True):
        self.connected = True
        self.players = FakePlayers(stream or {}, own_id)
        self.server_info = FakeServerInfo(spawn_response)
        self.stream = type("S", (), {"vehicles": {}, "pickups": {}})()
        self._rpc_spawn_queued = False
        self.spawn_calls = 0

    def send_rpc_spawn(self):
        self.spawn_calls += 1


class FakeApi:
    def __init__(self, pos):
        self._pos = pos

    def getbotposition(self):
        return self._pos


class FakeBot:
    def __init__(self, pos=(100.0, 200.0, 10.0), stream=None, own_id=1,
                 spawn_response=True):
        self.bot_id = 1
        self.api = FakeApi(pos)
        self.session = FakeSession(stream, own_id, spawn_response)
        self.logs = []

    def add_log(self, message):
        self.logs.append(message)


class StreamTests(unittest.TestCase):
    def test_self_is_not_a_witness(self):
        """Свой игрок лежит в стриме и раньше «подтверждал» мир сам собой."""
        bot = FakeBot(stream={1: FakePlayer("Me")}, own_id=1)
        self.assertEqual(real_world.streamed_players(bot), [])

    def test_npc_not_counted(self):
        bot = FakeBot(stream={2: FakePlayer("NPC", npc=True)})
        self.assertEqual(real_world.streamed_players(bot), [])

    def test_raw_byte_255_is_a_live_player(self):
        """В стриме поле NPC приходило байтом 255 у живых игроков.

        Проверка на истинность выкидывала всех подряд, бот считал мир
        пустым и «подтверждался» по чату, оставаясь невидимым.
        """
        bot = FakeBot(stream={2: FakePlayer("Live", npc=255)})
        names = [p["name"] for p in real_world.streamed_players(bot)]
        self.assertEqual(names, ["Live"])

    def test_explicit_one_is_npc(self):
        bot = FakeBot(stream={2: FakePlayer("Bot", npc=1)})
        self.assertEqual(real_world.streamed_players(bot), [])

    def test_real_players_counted(self):
        bot = FakeBot(stream={1: FakePlayer("Me"), 2: FakePlayer("Alpha")})
        names = [p["name"] for p in real_world.streamed_players(bot)]
        self.assertEqual(names, ["Alpha"])

    def test_probe_reports_flags(self):
        bot = FakeBot(stream={2: FakePlayer("Alpha")})
        report = real_world.probe(bot)
        self.assertTrue(report["connected"])
        self.assertTrue(report["spawn_response"])
        self.assertEqual(report["player_count"], 1)


class SpawnTests(unittest.TestCase):
    def test_finish_spawn_sends_once(self):
        bot = FakeBot()
        self.assertEqual(real_world.finish_spawn(bot), "sent")
        self.assertEqual(bot.session.spawn_calls, 1)
        # Второй раз — уже отправлено, повторный Spawn это кик кодом 25.
        self.assertEqual(real_world.finish_spawn(bot), "already")
        self.assertEqual(bot.session.spawn_calls, 1)

    def test_finish_spawn_waits_for_permission(self):
        bot = FakeBot(spawn_response=False)
        self.assertEqual(real_world.finish_spawn(bot), "not_allowed")
        self.assertEqual(bot.session.spawn_calls, 0)

    def test_finish_spawn_offline(self):
        bot = FakeBot()
        bot.session.connected = False
        self.assertEqual(real_world.finish_spawn(bot), "offline")
        self.assertEqual(bot.session.spawn_calls, 0)


class EnsureTests(unittest.TestCase):
    def test_confirmed_with_players(self):
        bot = FakeBot(stream={2: FakePlayer("Alpha"), 3: FakePlayer("Beta")})
        result = real_world.ensure(bot, timeout=1.0)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(result["players"]), 2)
        self.assertEqual(result["spawn"], "sent")

    def test_not_in_game_without_position(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        result = real_world.ensure(bot, timeout=0.2, enter_timeout=0.3)
        self.assertEqual(result["status"], "not_in_game")
        self.assertIsNone(result["position"])

    def test_unconfirmed_when_stream_stays_empty(self):
        bot = FakeBot()
        with mock.patch.object(real_world, "wait_players", return_value=[]):
            result = real_world.ensure(bot, timeout=0.2, fallback_warp=False)
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(result["players"], [])

    def test_warps_and_rechecks_when_stream_empty(self):
        bot = FakeBot()
        players = [{"id": 5, "name": "Gamma", "position": [0, 0, 0]}]
        with mock.patch.object(real_world, "wait_players",
                               side_effect=[[], players]):
            with mock.patch.object(real_world, "warp_to_station",
                                   return_value={"ok": True, "station": "Нижегородск 2"}):
                result = real_world.ensure(bot, timeout=0.2)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["station"], "Нижегородск 2")


class StatsTests(unittest.TestCase):
    def _bot(self):
        bot = mock.Mock()
        bot.level_num = None
        bot.exp_cur = None
        bot.exp_next = None
        bot.exp_left = None
        bot.level_str = "-"
        bot.stats_updated_at = 0.0
        bot.money = 0
        return bot

    def test_apply_stores_numbers_and_string(self):
        bot = self._bot()
        out = stats.apply(bot, {"level": 4, "exp_cur": 12, "exp_next": 40,
                                "exp_left": 28, "money": 900})
        self.assertTrue(out["ok"])
        self.assertEqual(bot.level_num, 4)
        self.assertEqual(bot.exp_left, 28)
        self.assertEqual(bot.level_str, "4 lvl · 12/40")
        self.assertEqual(bot.money, 900)
        self.assertGreater(bot.stats_updated_at, 0)

    def test_apply_without_level_is_refused(self):
        bot = self._bot()
        out = stats.apply(bot, {"level": None})
        self.assertFalse(out["ok"])

    def test_snapshot_percent(self):
        bot = self._bot()
        stats.apply(bot, {"level": 3, "exp_cur": 10, "exp_next": 40, "exp_left": 30})
        snap = stats.snapshot(bot)
        self.assertEqual(snap["exp_percent"], 25.0)
        self.assertEqual(snap["level_num"], 3)

    def test_format_level(self):
        self.assertEqual(stats.format_level(2, 5, 10), "2 lvl · 5/10")
        self.assertEqual(stats.format_level(2), "2 lvl")
        self.assertEqual(stats.format_level(None), "-")

    def test_refresh_times_out_without_menu(self):
        bot = self._bot()
        bot.session.connected = True
        bot.api.sendchat.return_value = None
        out = stats.refresh(bot, timeout=0.4)
        self.assertFalse(out["ok"])
        self.assertIn("не ответил", out["error"])
        bot.api.sendchat.assert_called_once_with("/mm")

    def test_refresh_detects_update(self):
        bot = self._bot()
        bot.session.connected = True

        def sendchat(_cmd):
            bot.stats_updated_at = 123.0
            bot.level_num = 5
            bot.exp_cur = 1
            bot.exp_next = 2
            bot.exp_left = 1

        bot.api.sendchat.side_effect = sendchat
        out = stats.refresh(bot, timeout=2.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["level_num"], 5)


class SpawnPermissionTests(unittest.TestCase):
    """Спавн ждёт разрешения сервера, а не первую позицию.

    Раньше ensure звал finish_spawn один раз сразу после появления
    координат. Сервер к этому моменту ещё не выставил spawn_response,
    ответ был not_allowed, и второго шанса не было: стрим оставался
    пустым, бот писал «мир не подтверждён».
    """

    def test_retries_until_allowed(self):
        bot = FakeBot()
        states = ["not_allowed", "not_allowed", "sent"]

        def fake_finish(_bot):
            return states.pop(0) if states else "sent"

        with mock.patch.object(real_world, "finish_spawn", fake_finish):
            state = real_world._wait_spawn_allowed(
                bot, time.monotonic() + 5.0)
        self.assertEqual(state, "sent")
        self.assertEqual(states, [], "должен был дойти до разрешения")

    def test_gives_up_when_server_never_allows(self):
        bot = FakeBot()
        with mock.patch.object(real_world, "finish_spawn",
                               return_value="not_allowed"):
            started = time.monotonic()
            state = real_world._wait_spawn_allowed(
                bot, time.monotonic() + 0.3)
        self.assertEqual(state, "not_allowed")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_offline_returns_immediately(self):
        bot = FakeBot()
        with mock.patch.object(real_world, "finish_spawn",
                               return_value="offline"):
            started = time.monotonic()
            state = real_world._wait_spawn_allowed(
                bot, time.monotonic() + 30.0)
        self.assertEqual(state, "offline")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_already_queued_is_not_resent(self):
        bot = FakeBot()
        with mock.patch.object(real_world, "finish_spawn",
                               return_value="already") as finish:
            state = real_world._wait_spawn_allowed(
                bot, time.monotonic() + 5.0)
        self.assertEqual(state, "already")
        self.assertEqual(finish.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
