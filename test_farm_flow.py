"""Ферма целиком, офлайн: проверяем, что уходит правильный ответ на меню.

Меню фермы приходит SA-MP диалогом style=5 (TABLIST_HEADERS). В такой
раскладке первая строка info — заголовок колонок, и list_item её НЕ
считает. Раньше это не учитывалось, и жёсткий индекс уезжал не туда.
Поэтому пункт ищется по тексту, а тесты проверяют именно текст.
"""

import time
import unittest

from logic import farm as farm_mod
from samp.constants import RPC
from samp.packets import PacketBuilder


MAIN_MENU = (
    "{E0584B}BLACK RUSSIA {FFFFFF}| Основное меню",
    "{E0584B}номер\t{E0584B}наименование\t{E0584B}доступное действие\n"
    "{E0584B}#1\t{FFFFFF}Информация о работах\t{AFAFAF}нажмите чтобы посмотреть\n"
    "{E0584B}#2\t{FFFFFF}Статистика фермера\t{AFAFAF}нажмите чтобы посмотреть\n"
    "{E0584B}#3\t{FFFFFF}Начать / Закончить работу\t{AFAFAF}нажмите для взаимодействия\n"
    "{E0584B}#4\t{FFFFFF}Аренда квадроциклов\t{AFAFAF}нажмите для подробностей",
    "Выбрать",
    "Закрыть",
)

JOB_MENU = (
    "{E0584B}BLACK RUSSIA {FFFFFF}| Выбор работы",
    "{E0584B}номер\t{E0584B}работа\n"
    "{E0584B}#1\t{FFFFFF}Сборщик урожая\n"
    "{E0584B}#2\t{FFFFFF}Пчеловод\n",
    "Выбрать",
    "Назад",
)

# Меню в другом порядке: сервер может переставить пункты, и поиск по
# тексту должен это пережить.
JOB_MENU_SHUFFLED = (
    "{E0584B}BLACK RUSSIA {FFFFFF}| Выбор работы",
    "{E0584B}номер\t{E0584B}работа\n"
    "{E0584B}#1\t{FFFFFF}Пчеловод\n"
    "{E0584B}#2\t{FFFFFF}Сборщик урожая\n",
    "Выбрать",
    "Назад",
)


class FakeSession:
    def __init__(self):
        self.answers = []
        self.json_sent = []
        self.connected = True

    def send_dialog_response(self, dialog_id, response=1, list_item=0, text=""):
        self.answers.append(
            {"id": dialog_id, "response": response, "item": list_item, "text": text}
        )
        return True

    def send_json(self, interface_id, payload, packet_name=""):
        self.json_sent.append((interface_id, payload, packet_name))
        return True


class FakeApi:
    def __init__(self, pos):
        self.pos = pos

    def getbotposition(self):
        return self.pos


class FakeStream:
    def __init__(self, pickups=None):
        self.pickups = pickups or {}

    def get_all_pickups(self):
        return self.pickups


class FakeBot:
    def __init__(self, pos=(1641.672, 1297.149, 12.140), pickups=None):
        self.session = FakeSession()
        self.session.stream = FakeStream(pickups)
        self.api = FakeApi(pos)


def make_runner(bot, travelled=None):
    moves = travelled if travelled is not None else []

    def travel(b, target):
        moves.append(target)
        b.api.pos = target

    runner = farm_mod.FarmRunner(bot, travel=travel, log=lambda m: None)
    runner.arrive_radius = 50.0  # на тесте не гоняем телепорт по карте
    return runner, moves


def open_main_menu(runner):
    """Довести раннер до состояния, где меню уже пришло."""
    runner.start()
    runner.step()  # to_farm -> to_npc
    runner.step()  # to_npc -> wait_menu
    runner.on_dialog(0, 5, *MAIN_MENU)
    runner.step()  # wait_menu -> pick_start


class ParseListDialogTests(unittest.TestCase):
    def test_header_row_is_not_counted(self):
        items = farm_mod.parse_list_dialog(MAIN_MENU[1], 5)
        self.assertEqual(len(items), 4)
        self.assertIn("Начать / Закончить работу", items[2])

    def test_colors_stripped(self):
        items = farm_mod.parse_list_dialog(MAIN_MENU[1], 5)
        for item in items:
            self.assertNotIn("{", item)
            self.assertNotIn("}", item)

    def test_plain_list_keeps_first_row(self):
        # style=2 — обычный список, заголовка нет, терять первую строку нельзя
        items = farm_mod.parse_list_dialog("Первый\nВторой", 2)
        self.assertEqual(items, ["Первый", "Второй"])

    def test_find_item_by_text(self):
        items = farm_mod.parse_list_dialog(MAIN_MENU[1], 5)
        self.assertEqual(farm_mod.find_item(items, "Начать / Закончить"), 2)

    def test_find_item_not_fooled_by_info_entry(self):
        """«Информация о работах» не должна ловиться на подстроку «работ»."""
        items = farm_mod.parse_list_dialog(MAIN_MENU[1], 5)
        self.assertEqual(
            farm_mod.find_item(items, farm_mod.MENU_START_WORK, farm_mod.MENU_START_WORK_ALT),
            2,
        )


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
        open_main_menu(runner)
        self.assertEqual(runner.state, farm_mod.S_PICK_START)

        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_PICK_JOB)
        self.assertEqual(len(bot.session.answers), 1)
        answer = bot.session.answers[0]
        # Пункт #3 в списке с нуля — это индекс 2.
        self.assertEqual(answer["item"], 2)
        self.assertEqual(answer["response"], 1)

    def test_job_menu_picks_harvester(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.step()
        runner.on_dialog(0, 5, *JOB_MENU)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_CAPTCHA)
        self.assertEqual(bot.session.answers[-1]["item"], 0)

    def test_job_menu_picks_by_text_not_position(self):
        """Пункты переставили — выбор должен остаться верным."""
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.step()
        runner.on_dialog(0, 5, *JOB_MENU_SHUFFLED)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_CAPTCHA)
        # «Сборщик урожая» теперь второй строкой -> индекс 1
        self.assertEqual(bot.session.answers[-1]["item"], 1)

    def test_second_menu_survives_being_queued(self):
        """Два меню подряд не должны затирать друг друга."""
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.on_dialog(0, 5, *JOB_MENU)  # пришло раньше, чем мы ответили
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_PICK_JOB)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_CAPTCHA)

    def test_missing_job_menu_waits_then_retries(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.step()
        state, _ = runner.step()
        # Сразу — ждём, сервер присылает второе меню с задержкой.
        self.assertEqual(state, farm_mod.S_PICK_JOB)
        runner._pick_job_at = time.time() - farm_mod.MENU_WAIT - 1
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_RETRY)
        self.assertNotEqual(state, farm_mod.S_FAILED)

    def test_expired_menu_reopens_instead_of_failing(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.dialogs.clear()  # меню протухло
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_WAIT_MENU)
        self.assertNotEqual(state, farm_mod.S_FAILED)

    def test_pickup_opens_menu_with_alt(self):
        """Подбор пикапа без ALT меню не открывает."""
        keys_pressed = []

        class Api(FakeApi):
            def SetKey(self, keys=0):
                keys_pressed.append(keys)

        bot = FakeBot(pickups={2048: {"x": 1641.3, "y": 1296.9, "z": 12.1}})
        bot.api = Api((1641.672, 1297.149, 12.140))
        bot.api.SendPickup = lambda pid: None
        bot.api.pickup_interact = lambda pid: None
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()  # to_farm -> to_npc
        runner.step()  # to_npc -> wait_menu
        runner.step()  # wait_menu -> подбор пикапа
        self.assertIn(1, keys_pressed, "ALT должен нажиматься вместе с подбором")
        self.assertIn(0, keys_pressed, "ALT должен отпускаться")


class FarmCaptchaTests(unittest.TestCase):
    def _runner_at_captcha(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        open_main_menu(runner)
        runner.step()
        runner.on_dialog(0, 5, *JOB_MENU)
        runner.step()  # -> captcha
        return bot, runner

    def test_captcha_with_text_is_solved_from_packet(self):
        bot, runner = self._runner_at_captcha()
        runner.on_dialog(0, 3, "ПРОВЕРКА", "tdHx", "Готово", "")
        state, note = runner.step()
        self.assertEqual(state, farm_mod.S_TO_WORK)
        self.assertEqual(bot.session.answers[-1]["text"], "tdHx")
        self.assertIn("tdHx", note)

    def test_captcha_challenge_is_reset_between_cycles(self):
        """На втором круге не должен уходить код с прошлого."""
        bot, runner = self._runner_at_captcha()
        runner.on_dialog(0, 3, "ПРОВЕРКА", "tdHx", "Готово", "")
        runner.step()
        self.assertIsNone(runner.challenge)
        # Новый круг: сервер снова открыл проверку
        runner.state = farm_mod.S_CAPTCHA
        runner.on_dialog(0, 3, "ПРОВЕРКА", "Qw9Z", "Готово", "")
        runner.step()
        self.assertEqual(bot.session.answers[-1]["text"], "Qw9Z")

    def test_captcha_without_text_retries_not_dies(self):
        bot, runner = self._runner_at_captcha()
        runner.on_dialog(0, 3, "ПРОВЕРКА", "Введите текст с картинки", "Готово", "")
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_RETRY)

    def test_no_captcha_waits_for_window_then_goes_to_work(self):
        """Капча приходит с задержкой — сразу уходить на работу нельзя."""
        bot, runner = self._runner_at_captcha()
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_CAPTCHA)
        runner._captcha_deadline = time.time() - 1
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_TO_WORK)


class FarmJsonMenuTests(unittest.TestCase):
    """Если сервер отдаст меню интерфейсом iid=10 — путь тоже должен жить."""

    def test_json_menu_is_answered_over_interface(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.start()
        runner.step()
        runner.step()
        runner.on_dialog_menu(
            ["Информация о работах", "Начать / Закончить работу"], False, {"o": 1}
        )
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_PICK_START)
        state, _ = runner.step()
        self.assertEqual(state, farm_mod.S_PICK_JOB)
        self.assertEqual(bot.session.json_sent[-1][0], 10)
        payload = bot.session.json_sent[-1][1]
        self.assertEqual(payload["i"], "1")

    def test_closed_json_menu_is_dropped(self):
        bot = FakeBot()
        runner, _ = make_runner(bot)
        runner.on_dialog_menu(["Начать / Закончить работу"], False, {})
        runner.on_dialog_menu([], True, {"c": 1})
        self.assertEqual(len(runner.menus), 0)


class FarmRpcTests(unittest.TestCase):
    def test_answer_uses_fixed_dialog_rpc(self):
        """Раньше ответ уходил как RPC 2 из-за id 514 — меню молчали."""
        packet = PacketBuilder.rpc_build("dialog_response", 0, 1, 2, "")
        self.assertEqual(packet[1], RPC.DIALOG_RESPONSE)
        self.assertEqual(RPC.DIALOG_RESPONSE, 62)


if __name__ == "__main__":
    unittest.main(verbosity=2)
