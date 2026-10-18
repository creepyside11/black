"""Регрессии на баги, найденные в аудите. Без живого сервера."""

import re
import unittest

from samp.constants import RPC
from samp.packets import PacketBuilder, RPCBuilder


class RpcIdEncodingTests(unittest.TestCase):
    def test_dialog_response_fits_in_9_bits(self):
        # 514 не влезает в 9 бит и уходил как RPC 2 — меню не отвечали.
        self.assertLessEqual(RPC.DIALOG_RESPONSE, 511)
        self.assertEqual(RPC.DIALOG_RESPONSE, 62)

    def test_dialog_response_packet_uses_right_id(self):
        packet = PacketBuilder.rpc_build("dialog_response", 0, 1, 2, "")
        self.assertEqual(packet[0], 0x0B)
        # 62 = 0x3E младшие 8 бит, старший бит 0, дальше 0b11
        self.assertEqual(packet[1], 62)

    def test_guard_rejects_overflow(self):
        for bad in (512, 514, 515, 517, 1024, -1):
            with self.assertRaises(ValueError):
                RPCBuilder.create(bad, b"")

    def test_guard_accepts_boundaries(self):
        for good in (0, 1, 62, 347, 448, 510, 511):
            RPCBuilder.create(good, b"")

    def test_no_outbound_constant_overflows(self):
        offenders = []
        for name in dir(RPC):
            if name.startswith("_"):
                continue
            value = getattr(RPC, name)
            if isinstance(value, int) and value > 511:
                offenders.append((name, value))
        # Серверные id тоже должны влезать: разбор идёт по тем же 9 битам.
        self.assertEqual(offenders, [], f"id > 511: {offenders}")


class BanTriggerTests(unittest.TestCase):
    """Слово 'работы' содержит 'бот' — на этом ферма уходила в ложный бан."""

    PATTERNS = (
        re.compile(r"\bбот(?:ы|а|ов|ом|е)?\b", re.IGNORECASE),
        re.compile(r"\bчит(?:ы|ов|ами|ер)?\b", re.IGNORECASE),
    )

    def hits(self, text):
        return any(p.search(text) for p in self.PATTERNS)

    def test_work_menu_is_not_a_ban(self):
        self.assertFalse(self.hits("Информация о работах нажмите чтобы посмотреть"))
        self.assertFalse(self.hits("Начать / Закончить работу"))
        self.assertFalse(self.hits("Сборщик урожая"))
        self.assertFalse(self.hits("работа на ферме"))

    def test_real_ban_still_caught(self):
        self.assertTrue(self.hits("Вы бот, доступ запрещён"))
        self.assertTrue(self.hits("использование бота запрещено"))
        self.assertTrue(self.hits("подозрение в использовании чит-программ"))
        self.assertTrue(self.hits("читы обнаружены"))

    def test_ban_triggers_list_has_no_bare_bot(self):
        with open("bot.py", encoding="utf-8") as fh:
            src = fh.read()
        start = src.index("ban_triggers = [")
        end = src.index("]", start)
        block = src[start:end]
        self.assertNotIn('"бот"', block)
        self.assertNotIn('"читы"', block)


class HandlerSignatureTests(unittest.TestCase):
    """_emit_callback подставляет объект бота первым аргументом."""

    def test_all_handlers_take_bot_first(self):
        with open("bot.py", encoding="utf-8") as fh:
            src = fh.read()
        broken = []
        for match in re.finditer(
            r'@emitter\.on\("([^"]+)"\)\s*\n\s*def (\w+)\(([^)]*)\)', src
        ):
            event, func, params = match.group(1), match.group(2), match.group(3)
            first = params.split(",")[0].strip()
            if not (first.startswith("bot_obj") or first.startswith("*")):
                broken.append((event, func, first))
        self.assertEqual(broken, [], f"обработчики без bot_obj: {broken}")


class SpawnDeliveryTests(unittest.TestCase):
    def test_spawn_uses_reliable_system_priority(self):
        from samp.sender import RPC_DELIVERY, REL, PRI_SYS

        self.assertEqual(RPC_DELIVERY["spawn"], (REL, PRI_SYS, 0))

    def test_dialog_response_uses_system_priority(self):
        from samp.sender import RPC_DELIVERY, PRI_SYS

        self.assertEqual(RPC_DELIVERY["dialog_response"][1], PRI_SYS)


class RegistrationSpawnPathTests(unittest.TestCase):
    def test_request_spawn_rpc_does_not_exist(self):
        # В registration.py на него ссылались, но такого RPC нет — путь спавна
        # молча падал в except: pass.
        from samp.packets import RPC_BUILDERS

        self.assertNotIn("request_spawn", RPC_BUILDERS)

    def test_registration_module_imports(self):
        import importlib

        module = importlib.import_module("logic.registration")
        self.assertTrue(hasattr(module, "RegistrationConfig"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
