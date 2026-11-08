"""Тесты отправки капчи во внешний сервис. Без сети: транспорт подменяется."""
import unittest

import logic.captcha_api as A
from logic.captcha import KIND_IMAGE_URL, from_interface


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
JPEG = b"\xff\xd8" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 8


class MimeTests(unittest.TestCase):
    def test_detects_supported_formats(self):
        self.assertEqual(A.detect_mime(PNG), "image/png")
        self.assertEqual(A.detect_mime(JPEG), "image/jpeg")
        self.assertEqual(A.detect_mime(WEBP), "image/webp")

    def test_rejects_unknown(self):
        self.assertIsNone(A.detect_mime(b"GIF89a" + b"\x00" * 8))
        self.assertIsNone(A.detect_mime(b""))


class SolveTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = A._post

    def tearDown(self):
        A._post = self._orig

    def _stub(self, *responses):
        queue = list(responses)

        def fake(url, key, payload, timeout):
            self.calls.append({"url": url, "key": key, "payload": payload, "timeout": timeout})
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        A._post = fake

    def test_returns_answer(self):
        self._stub({"answer": "VaLA", "request_id": "x"})
        self.assertEqual(A.solve_bytes(PNG, api_key="rko_test"), "VaLA")
        self.assertEqual(self.calls[0]["payload"]["mime_type"], "image/png")
        self.assertTrue(self.calls[0]["key"].startswith("rko_"))

    def test_trims_wrapping(self):
        self._stub({"answer": "\n VaLA \n"})
        self.assertEqual(A.solve_bytes(PNG, api_key="k"), "VaLA")

    def test_unreadable_answer_returns_none(self):
        self._stub({"answer": "UNREADABLE"})
        self.assertIsNone(A.solve_bytes(PNG, api_key="k"))

    def test_empty_image_returns_none(self):
        self.assertIsNone(A.solve_bytes(b"", api_key="k"))
        self.assertEqual(self.calls, [])

    def test_oversized_image_returns_none(self):
        big = PNG + b"\x00" * (A.MAX_IMAGE_BYTES + 1)
        self.assertIsNone(A.solve_bytes(big, api_key="k"))
        self.assertEqual(self.calls, [])

    def test_timeout_retried_once(self):
        self._stub(A.RakOCRError("таймаут"), {"answer": "VaLA"})
        self.assertEqual(A.solve_bytes(PNG, api_key="k", retries=1), "VaLA")
        self.assertEqual(len(self.calls), 2)

    def test_auth_error_not_retried(self):
        self._stub(A.RakOCRError("HTTP 401: Invalid API key"))
        self.assertIsNone(A.solve_bytes(PNG, api_key="k", retries=2))
        self.assertEqual(len(self.calls), 1)

    def test_timeout_default_is_generous(self):
        # Ответ модели может идти до минуты.
        self.assertGreaterEqual(A.SOLVE_TIMEOUT, 60.0)


class SolverTests(unittest.TestCase):
    def test_ignores_other_kinds(self):
        ch = from_interface(13, {"i": "ab12"})
        self.assertIsNone(A.RakOCRSolver().solve(ch))

    def test_uses_cached_image(self):
        ch = from_interface(26, {"u": "https://x/y.png"})
        ch.image = PNG
        orig = A.solve_bytes
        seen = {}

        def fake(image, **kw):
            seen["image"] = image
            return "VaLA"

        A.solve_bytes = fake
        try:
            self.assertEqual(A.RakOCRSolver().solve(ch), "VaLA")
        finally:
            A.solve_bytes = orig
        self.assertEqual(seen["image"], PNG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
