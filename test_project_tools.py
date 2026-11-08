"""Инструменты проектов: вердикт проверки входа.

По вердикту решается, каким путём идти с проектом, поэтому он не должен
врать: при --only считается только то, что реально спросили у сервера.
"""

import importlib.util
import os
import unittest


def _load(name: str):
    root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, "tools", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handshake_tool = _load("project_handshake")


class VerdictTests(unittest.TestCase):
    def test_raknet_only(self):
        text = handshake_tool.verdict_for(False, True)
        self.assertIn("стандартный RakNet", text)
        self.assertNotIn("нет ответа", text)

    def test_br_only(self):
        text = handshake_tool.verdict_for(True, False)
        self.assertIn("только BR-хендшейк", text)

    def test_both(self):
        self.assertIn("и на BR, и на стандартный RakNet",
                      handshake_tool.verdict_for(True, True))

    def test_silence_mentions_udp(self):
        """Молчание — это чаще сеть, чем протокол: вердикт обязан это сказать."""
        text = handshake_tool.verdict_for(False, False)
        self.assertIn("UDP", text)
        self.assertIn("BR-сервер", text)

    def test_only_br_does_not_claim_raknet_result(self):
        text = handshake_tool.verdict_for(True, False, only="br")
        self.assertIn("принят", text)
        text = handshake_tool.verdict_for(False, False, only="br")
        self.assertIn("RakNet не трогали", text)

    def test_only_raknet_does_not_claim_br_result(self):
        text = handshake_tool.verdict_for(False, True, only="raknet")
        self.assertIn("CONNECTION_REQUEST_ACCEPTED", text)
        text = handshake_tool.verdict_for(False, False, only="raknet")
        self.assertIn("проверялся только он", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
