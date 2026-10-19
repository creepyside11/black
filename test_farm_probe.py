"""Матрица вариантов меню фермы: сборка и разбор результата."""

import unittest

from logic import farm_probe as probe


class VariantTests(unittest.TestCase):
    def test_close_variant_comes_first(self):
        """Контроль доставки проверяем раньше индексов."""
        variants = probe.build_variants(4)
        self.assertEqual(variants[0]["name"], "close")
        self.assertEqual(variants[0]["response"], probe.RESPONSE_BUTTON2)

    def test_both_indexing_hypotheses_present(self):
        names = [v["name"] for v in probe.build_variants(4)]
        self.assertIn("idx2", names, "заголовок не в счёте")
        self.assertIn("idx3", names, "заголовок в счёте")

    def test_variants_are_unique(self):
        variants = probe.build_variants(4)
        seen = set()
        for variant in variants:
            key = (variant["response"], variant["item"])
            self.assertNotIn(key, seen, f"дубль варианта {variant['name']}")
            seen.add(key)

    def test_every_variant_explains_itself(self):
        for variant in probe.build_variants(4):
            self.assertTrue(variant["why"], f"{variant['name']} без объяснения")

    def test_small_menu_does_not_get_out_of_range_items(self):
        variants = probe.build_variants(2)
        for variant in variants:
            self.assertLessEqual(variant["item"], 4)


class ClassifyTests(unittest.TestCase):
    def test_silence(self):
        self.assertEqual(probe.classify(None), "silence")
        self.assertEqual(probe.classify({}), "silence")

    def test_closed(self):
        self.assertEqual(
            probe.classify({"dialog": {"title": "", "info": ""}}), "delivered_closed"
        )

    def test_follow_up_dialog(self):
        self.assertEqual(
            probe.classify({"dialog": {"title": "Выбор работы", "info": "..."}}),
            "follow_up",
        )

    def test_chat_counts_as_reaction(self):
        """Устроиться могли ответить строкой в чат, а не диалогом."""
        self.assertEqual(
            probe.classify({"chat": ["Вы устроились на работу"]}), "chat"
        )

    def test_json_counts_as_reaction(self):
        self.assertEqual(probe.classify({"json": (13, {"t": 1})}), "follow_up")


class SummariseTests(unittest.TestCase):
    def _attempt(self, name, item, response, outcome, detail=""):
        return {
            "name": name, "item": item, "response": response,
            "outcome": outcome, "detail": detail, "why": "",
        }

    def test_finds_working_variant(self):
        attempts = [
            self._attempt("close", 0, 0, "silence"),
            self._attempt("idx2", 2, 1, "silence"),
            self._attempt("idx3", 3, 1, "follow_up", "Выбор работы"),
        ]
        report = probe.summarise(attempts)
        self.assertIn("РАБОЧИЙ ВАРИАНТ", report)
        self.assertIn("list_item=3", report)

    def test_reports_transport_problem(self):
        attempts = [self._attempt("close", 0, 0, "silence")]
        report = probe.summarise(attempts)
        self.assertIn("транспорт", report)

    def test_reports_delivery_ok_but_no_transition(self):
        attempts = [
            self._attempt("close", 0, 0, "delivered_closed"),
            self._attempt("idx2", 2, 1, "silence"),
        ]
        report = probe.summarise(attempts)
        self.assertIn("Доставка верна", report)

    def test_empty(self):
        self.assertIn("не было", probe.summarise([]))


class DialogRowsTests(unittest.TestCase):
    def test_rows_skip_blank_lines(self):
        self.assertEqual(probe.dialog_rows("a\n\n b \n"), ["a", " b "])


if __name__ == "__main__":
    unittest.main(verbosity=2)
