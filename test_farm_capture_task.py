import unittest
from unittest.mock import patch

from logic.farm import S_MINIGAME, S_RETRY
from logic.farm_task import FarmTask


class Api:
    def __init__(self):
        self.sent = []
    def SendRPC(self, rpc, payload):
        self.sent.append((rpc, payload))


class Bot:
    def __init__(self):
        self.api = Api()
        self.session = object()
    def add_log(self, message):
        pass


class FarmCaptureTaskTest(unittest.TestCase):
    def setUp(self):
        self.bot = Bot()
        self.task = FarmTask(self.bot)
        self.task.running = True
        self.task.runner.state = S_MINIGAME

    def test_only_live_apple_show_triggers_selection(self):
        apple = bytes.fromhex('0408') + b'\0' * 70 + b'\x14\x00gui:fruitpickerapple'
        self.task.on_receive_rpc(349, apple)
        self.assertEqual(self.bot.api.sent, [])
        self.task.on_receive_rpc(418, apple)
        self.assertEqual(self.bot.api.sent, [(437, bytes.fromhex('0408'))])
        self.task.on_receive_rpc(418, b'\x04\x08' + b'\0' * 70 + b'\x14\x00gui:fruitpickerfield')
        self.assertEqual(len(self.bot.api.sent), 1)

    def test_real_counter_controls_delivery(self):
        text = b'2.0_/_5.0_kg'
        self.task.on_receive_rpc(349, b'\x0d\x08' + len(text).to_bytes(2, 'little') + text)
        self.assertEqual(self.task.runner.kg, 2.0)
        self.task._last_apple_at = 1
        self.assertFalse(self.task._minigame(self.task.runner))
        text = b'5.0_/_5.0_kg'
        self.task.on_receive_rpc(349, b'\x0d\x08' + len(text).to_bytes(2, 'little') + text)
        with patch('logic.farm_task.time.monotonic', return_value=3):
            self.assertTrue(self.task._minigame(self.task.runner))

    def test_timeout_does_not_assume_harvest_succeeded(self):
        self.task.runner.minigame_started_at = 1
        with patch('logic.farm.time.time', return_value=300):
            self.task.runner.step()
        self.assertEqual(self.task.runner.state, S_RETRY)
        self.assertEqual(self.task.runner.cycles, 0)

if __name__ == '__main__':
    unittest.main()
