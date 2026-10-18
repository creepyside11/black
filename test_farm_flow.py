"""Ферма целиком, офлайн: проверяем, что уходит правильный ответ на меню."""

import unittest

from logic import farm as farm_mod
from samp.constants import RPC
from samp.packets import PacketBuilder


MAIN_MENU = (
    "{E0584B}BLACK RUSSIA {FFFFFF}| Основное меню",
    "{E0584B}номер\t{E0584B}наименование\t{E0584B}доступное действие\n"
    "{E0584B}#1\t{FFFFFF}Информация о работах\t{AFAFAF}нажмите чтобы посмотреть\n"
    "{E0584B}#2\t{FFFFFF}Статистика фермера\t{AFAFAF}нажмите чтобы посмотреть\n"
    "{E0584B}#3\t{FFFFFF}Начать / Закончить работу\t{AFAFAF}нажмите для взаимодействия\n",
    "Выбрать",
    "Закрыть",
)

JOB_MENU = (
    "{E0584B}BLACK RUSSIA {FFFFFF}| Выбор работы",
    "{E0584B}#1\t{FFFFFF}Сборщик урожая\n"
    "{E0584B}#2\t{FFFFFF}Пчеловод\n",
    "Выбрать",
    "Назад",
)


class FakeSession:
    def __init__(self):
        self.answers = []
        self.connected = True

    def send_dialog_response(self, dialog_id, response=1, list_item=0, text=""):
        self.answers.append(
            {"id": dialog_id, "response": response, "item": list_item, "text": text}
        )
        return True


class FakeApi:
    def __init__(self, pos):
        self.pos = pos

    def getbotposition(self):
        return self.pos


class FakeBot:
    def __init__(self, pos=(1641.672, 1297.149, 12.140)):
        self.session = FakeSession()
        self.api = FakeApi(pos)


def make_runner(bot, travelled=None):
    moves = travelled if travelled is not None else []

    def travel(b, target):
        moves.append(target)
        b.api.pos = target

    runner = farm_mod.FarmRunner(bot, travel=travel, log=lambda m: None)
    runner.arrive_radius = 50.0  # на тесте не гоняем телепорт по карте
    return runner, moves


class FarmMenuFlowTests(unittest.TestCase):
    def test_start_moves_to_farm(self):
        bot = FakeBot(pos=(0.0, 0.0, 0.0))
        runner, moves = make_runner(bot)
        runner.start()
        self.assertEqual(runner.state, farm_mod.S_TO_FARM)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_TO_NPC)
        self.assertEqual(moves[-1], farm_mod.FARM_LOCATION)

    def test_main_menu_answered_with_item_index_2(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()  # to_npc
        runner.step()  # wait_menu
        runner.on_dialog(0, 5, *MAIN_MENU)
        state, _ = runner.step()  # wait_menu -> pick_start
        self.assertEqual(state, farm_mod.S_PICK_START)

        state, _ = runner.step()  # pick_start -> ответ
        self.assertEqual(state, farm_mod.S_PICK_JOB)
        self.assertEqual(len(bot.session.answers), 1)
        answer = bot.session.answers[0]
        # Пункт 3 в списке с нуля — это индекс 2.
        self.assertEqual(answer["item"], 2)
        self.assertEqual(answer["response"], 1)

    def test_job_menu_picks_harvester(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *MAIN_MENU)
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *JOB_MENU)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_CAPTCHA)
        self.assertEqual(bot.session.answers[-1]["item"], 0)

    def test_missing_job_menu_reopens_instead_of_failing(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *MAIN_MENU)
        runner.step()
        runner.step()
        # Второе меню не пришло — должны вернуться к открытию, а не упасть.
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_WAIT_MENU)
        self.assertNotEqual(state, farm_mod.S_FAILED)

    def test_expired_dialog_does_not_fail(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *MAIN_MENU)
        runner.dialog = None  # меню закрылось само
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_WAIT_MENU)

    def test_captcha_with_text_is_solved_from_packet(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *MAIN_MENU)
        runner.step()
        runner.step()
        runner.on_dialog(0, 5, *JOB_MENU)
        runner.step()  # -> captcha
        runner.on_dialog(0, 3, "ПРОВЕРКА", "tdHx", "Готово", "")
        state, note = runner.step()
        self.assertEqual(state, farm_mod.S_TO_WORK)
        self.assertEqual(bot.session.answers[-1]["text"], "tdHx")
        self.assertIn("tdHx", note)

    def test_captcha_without_text_needs_operator(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.state = farm_mod.S_CAPTCHA
        runner.on_dialog(0, 3, "ПРОВЕРКА", "Введите текст с картинки", "Готово", "")
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_FAILED)


class FarmRpcTests(unittest.TestCase):
    def test_answer_uses_fixed_dialog_rpc(self):
        """Раньше ответ уходил как RPC 2 из-за id 514 — меню молчали."""
        packet = PacketBuilder.rpc_build("dialog_response", 0, 1, 2, "")
        self.assertEqual(packet[1], RPC.DIALOG_RESPONSE)
        self.assertEqual(RPC.DIALOG_RESPONSE, 62)


if __name__ == "__main__":
    unittest.main(verbosity=2)
