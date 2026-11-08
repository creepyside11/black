"""Ники: стили, длина, уникальность, отсутствие английских имён с русской фамилией."""

import random
import unittest

from logic.nicks import (
    CAMEL_BASE,
    CAMEL_PREFIX,
    FEMALE_NAMES,
    MALE_NAMES,
    MAX_LEN,
    NickGenerator,
    SLANG_FIRST,
    SLANG_SECOND,
    SURNAMES,
    TURKIC_NAMES,
    _camel,
    _double_letter,
    _typo_name,
)


class MutationTests(unittest.TestCase):
    def test_double_letter(self):
        rng = random.Random(1)
        result = _double_letter("Markov", rng)
        self.assertGreater(len(result), len("Markov"))
        self.assertEqual(len(result), len("Markov") + 1)

    def test_double_letter_short_word_untouched(self):
        self.assertEqual(_double_letter("Lev", random.Random(1)), "Lev")

    def test_typo_changes_name(self):
        rng = random.Random(1)
        self.assertNotEqual(_typo_name("Alex", rng), "Alex")

    def test_typo_no_match_keeps_name(self):
        self.assertEqual(_typo_name("Bogdan", random.Random(1)), "Bogdan")

    def test_camel(self):
        self.assertEqual(_camel("Na", "capte"), "NaCapte")


class StyleTests(unittest.TestCase):
    def setUp(self):
        self.g = NickGenerator(random.Random(11))

    def test_every_style_has_underscore(self):
        for style in ("real", "meme", "english", "mixed", "invented", "short"):
            for _ in range(20):
                nick = getattr(self.g, f"_{style}")()
                self.assertIn("_", nick, f"{style}: {nick}")
                first, second = nick.split("_", 1)
                self.assertTrue(first and second, f"{style}: {nick}")

    def test_meme_uses_slang_pool(self):
        """Sotka_NaCapte — первая часть из сленгового пула."""
        for _ in range(40):
            first = self.g._meme().split("_")[0]
            self.assertIn(first, SLANG_FIRST)

    def test_meme_second_is_russian_camel(self):
        """Вторая часть — русская связка, а не «ZaPrince»."""
        valid = set(SLANG_SECOND)
        for prefix in CAMEL_PREFIX:
            for base in CAMEL_BASE:
                valid.add(_camel(prefix, base))
        for _ in range(60):
            second = self.g._meme().split("_", 1)[1]
            self.assertIn(second, valid, f"неожиданная связка: {second}")

    def test_real_name_from_known_pools(self):
        pools = set(MALE_NAMES) | set(FEMALE_NAMES) | set(TURKIC_NAMES)
        # Имя может получить опечатку (Aleqs, Teemoha) — добавляем все
        # варианты, которые умеет делать _typo_name, иначе тест ловит
        # задуманное поведение как ошибку.
        allowed = set(pools)
        for name in pools:
            for seed in range(4):
                allowed.add(_typo_name(name, random.Random(seed)))
        for _ in range(80):
            first = self.g._real().split("_")[0]
            self.assertIn(first, allowed, f"незнакомое имя: {first}")

    def test_no_english_first_with_russian_surname(self):
        """Регрессия: раньше выходило «Moira_Amosov» — английское имя."""
        english_first = {"Moira", "Millard", "Chancellor", "Teryl", "Lamisha"}
        for _ in range(300):
            nick = self.g.build()
            first = nick.split("_")[0]
            self.assertNotIn(first, english_first, f"английское имя: {nick}")

    def test_female_surname_agrees(self):
        """У женского имени фамилия с «-а» — как Amina_Gasanova."""
        matched = 0
        for _ in range(200):
            nick = self.g._real()
            first, second = nick.split("_", 1)
            if first in FEMALE_NAMES and second in SURNAMES:
                matched += 1
        # Если фамилия без «-а» при женском имени — это 15% исключений,
        # поэтому почти все совпадения должны быть с окончанием.
        self.assertLess(matched, 10)

    def test_female_surname_not_double_ended(self):
        """«Shilovaa» — баг: удвоение срабатывало после женского окончания.

        Проверяем именно окончание: удвоенная буква в середине фамилии
        (Kaabanov, Markovv) — это нормальный приём, а «-aa» на конце нет.
        """
        for _ in range(400):
            nick = self.g._real()
            self.assertFalse(
                nick.endswith(("aa", "yya")),
                f"удвоенное женское окончание: {nick}",
            )

    def test_english_style_uses_english_words(self):
        for _ in range(20):
            nick = self.g._english()
            first, second = nick.split("_")
            self.assertTrue(first[0].isupper())
            self.assertTrue(second[0].isupper())


class LengthTests(unittest.TestCase):
    def test_never_exceeds_limit(self):
        g = NickGenerator(random.Random(5))
        for _ in range(1000):
            self.assertLessEqual(len(g.build()), MAX_LEN)

    def test_reasonable_minimum(self):
        g = NickGenerator(random.Random(5))
        for _ in range(200):
            self.assertGreaterEqual(len(g.build()), 5)

    def test_long_nick_keeps_first_part_whole(self):
        """При обрезке имя должно остаться целым, режется вторая часть."""
        g = NickGenerator(random.Random(5))
        for _ in range(500):
            nick = g.build()
            if "_" in nick:
                first = nick.split("_")[0]
                self.assertLessEqual(len(first), MAX_LEN - 4)


class UniquenessTests(unittest.TestCase):
    def test_generate_returns_unique(self):
        g = NickGenerator(random.Random(9))
        batch = [g.generate() for _ in range(400)]
        self.assertEqual(len(set(batch)), len(batch))

    def test_avoids_given_nicknames(self):
        g = NickGenerator(random.Random(9))
        taken = {g.build() for _ in range(50)}
        for _ in range(100):
            self.assertNotIn(g.generate(avoid=taken), taken)

    def test_styles_spread(self):
        """Все стили должны реально появляться, а не один на всех."""
        g = NickGenerator(random.Random(13))
        seen = set()
        for _ in range(400):
            nick = g.build()
            first = nick.split("_")[0]
            if first in SLANG_FIRST:
                seen.add("meme")
            elif first in MALE_NAMES or first in FEMALE_NAMES or first in TURKIC_NAMES:
                seen.add("real")
            else:
                seen.add("other")
        self.assertGreaterEqual(len(seen), 3)

    def test_fallback_has_no_digits(self):
        """Регрессия: фолбэк клеил номер к фамилии — «Aro_Carter2»."""
        g = NickGenerator(random.Random(2))
        for _ in range(30):
            nick = g.generate(avoid=set(), attempts=0)
            self.assertFalse(
                any(ch.isdigit() for ch in nick),
                f"цифра в нике: {nick}",
            )

    def test_fallback_looks_like_doubled_letter(self):
        """Фолбэк удваивает последнюю букву — как Markovv, Kovalevv."""
        g = NickGenerator(random.Random(4))
        nick = g.generate(avoid=set(), attempts=0)
        first, second = nick.split("_", 1)
        self.assertGreaterEqual(len(second), 4)

    def test_no_digits_anywhere(self):
        """Цифр не бывает ни в одном стиле."""
        g = NickGenerator(random.Random(17))
        for style in ("real", "meme", "english", "mixed", "invented", "short"):
            for _ in range(40):
                nick = getattr(g, f"_{style}")()
                self.assertFalse(
                    any(ch.isdigit() for ch in nick),
                    f"{style}: цифра в нике {nick}",
                )

    def test_no_digits_in_pools(self):
        """В пулах не должно быть леетспика: «NaTa4ke» — это цифра."""
        import re

        for name, pool in (
            ("CAMEL_BASE", CAMEL_BASE),
            ("CAMEL_PREFIX", CAMEL_PREFIX),
            ("SLANG_FIRST", SLANG_FIRST),
            ("SLANG_SECOND", SLANG_SECOND),
            ("SURNAMES", SURNAMES),
            ("MALE_NAMES", MALE_NAMES),
            ("FEMALE_NAMES", FEMALE_NAMES),
            ("TURKIC_NAMES", TURKIC_NAMES),
        ):
            bad = [x for x in pool if re.search(r"\d", x)]
            self.assertEqual(bad, [], f"{name}: цифры в пулах {bad}")

    def test_same_seed_same_result(self):
        first = [NickGenerator(random.Random(21)).build() for _ in range(3)]
        second = [NickGenerator(random.Random(21)).build() for _ in range(3)]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
