"""Тесты капчи. Без сети и без аккаунта: картинка синтетическая,
HTTP-сервер локальный, сессия — заглушка.
"""
import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest

import logic.captcha as C


class FakeSession:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send_json(self, interface_id, data, **kw):
        if not self.connected:
            return False
        self.sent.append((interface_id, data))
        return True


class FakeBot:
    def __init__(self, session=None):
        self.session = session or FakeSession()
        self.logs = []

    def add_log(self, text):
        self.logs.append(str(text))


class OCRConfidenceTests(unittest.TestCase):
    def test_low_confidence_is_not_sent_as_text(self):
        from unittest.mock import patch
        with patch('captcha_solver.solve', return_value={
            'text': 'lrlj', 'confidence': 0.5069, 'method': 'template'
        }):
            with self.assertRaisesRegex(ValueError, 'требует проверки'):
                C.solve_image(b'not-used-by-mock')

    def test_high_confidence_preserves_case(self):
        from unittest.mock import patch
        with patch('captcha_solver.solve', return_value={
            'text': 'Ab9Z', 'confidence': 0.95, 'method': 'template'
        }):
            self.assertEqual(C.solve_image(b'not-used-by-mock'), 'Ab9Z')


class ProtocolTests(unittest.TestCase):
    """Формат, снятый с байткода BrNewCaptcha."""

    def test_interface_is_26(self):
        self.assertEqual(C.CAPTCHA_INTERFACE, 26)

    def test_answer_payload(self):
        self.assertEqual(C.answer_payload("ab12"), {"t": 0, "s": "ab12"})

    def test_answer_type_zero(self):
        self.assertEqual(C.answer_payload("x")["t"], 0)

    def test_answer_text_is_str(self):
        self.assertIsInstance(C.answer_payload(1234)["s"], str)

    def test_url_from_u(self):
        url = "https://cdn.example/cap.png"
        ch = C.from_interface(26, {"o": 1, "u": url})
        self.assertIsNotNone(ch)
        self.assertEqual(ch.kind, C.KIND_IMAGE_URL)
        self.assertEqual(ch.image_url, url)

    def test_26_without_url_is_not_a_challenge(self):
        for bad in ({}, {"o": 1}, {"u": ""}, {"u": "ftp://x"}, {"u": 42}):
            self.assertIsNone(C.from_interface(26, bad), bad)

    def test_t_is_not_the_answer(self):
        # В BR "t" — тип сообщения, а не текст ответа.
        self.assertIsNone(C.from_interface(26, {"t": 100}))

    def test_field_limit_matches_client(self):
        self.assertEqual(C.FIELD_MAX_LEN, 20)


class OldPathsStillWorkTests(unittest.TestCase):
    """Старые виды капчи не должны сломаться от новых правок."""

    def test_packet_text_still_parsed(self):
        ch = C.from_interface(13, {"i": "ab12"})
        self.assertIsNotNone(ch)
        self.assertEqual(ch.kind, C.KIND_PACKET_TEXT)
        self.assertEqual(ch.text, "ab12")

    def test_texture_model_still_parsed(self):
        ch = C.from_interface(13, {"m": 5})
        self.assertIsNotNone(ch)
        self.assertEqual(ch.kind, C.KIND_TEXTURE)

    def test_dialog_path_untouched(self):
        ch = C.from_dialog(0, "ПРОВЕРКА", "ab12")
        self.assertIsNotNone(ch)
        self.assertEqual(ch.text, "ab12")

    def test_short_answer_limit_for_packets(self):
        self.assertTrue(C.looks_like_answer("ab12"))
        self.assertFalse(C.looks_like_answer("abcdefghij"))

    def test_long_answer_allowed_for_image(self):
        self.assertTrue(C.looks_like_answer("abcdefghij", image=True))
        self.assertFalse(C.looks_like_answer("a" * 30, image=True))


class ImageSolverTests(unittest.TestCase):
    def test_solves_via_injected_ocr(self):
        ch = C.from_interface(26, {"u": "https://x/1.png"})
        solver = C.ImageUrlSolver(
            download=lambda url: b"img", ocr=lambda img, charset=None: "ab12"
        )
        self.assertEqual(solver.solve(ch), "ab12")
        self.assertEqual(ch.image, b"img")

    def test_download_error_returns_none(self):
        ch = C.from_interface(26, {"u": "https://x/2.png"})

        def boom(url):
            raise OSError("нет сети")

        solver = C.ImageUrlSolver(download=boom, ocr=lambda i, charset=None: "ab12")
        self.assertIsNone(solver.solve(ch))
        self.assertIn("нет сети", ch.error)

    def test_empty_ocr_returns_none(self):
        ch = C.from_interface(26, {"u": "https://x/3.png"})
        solver = C.ImageUrlSolver(
            download=lambda url: b"img", ocr=lambda i, charset=None: ""
        )
        self.assertIsNone(solver.solve(ch))

    def test_ignores_other_kinds(self):
        ch = C.from_interface(13, {"i": "ab12"})
        solver = C.ImageUrlSolver(
            download=lambda url: b"img", ocr=lambda i, charset=None: "ab12"
        )
        self.assertIsNone(solver.solve(ch))


class AnswererTests(unittest.TestCase):
    def _answerer(self, text="ab12", **kw):
        chain = C.SolverChain([C.ImageUrlSolver(
            download=lambda url: b"img", ocr=lambda i, charset=None: text
        )])
        return C.CaptchaAnswerer(chain=chain, delay=(0.0, 0.0), **kw)

    def test_sends_to_26_with_t0(self):
        sess = FakeSession()
        rep = self._answerer().handle(sess, C.from_interface(26, {"u": "https://x/a.png"}))
        self.assertTrue(rep["sent"], rep)
        self.assertEqual(sess.sent, [(26, {"t": 0, "s": "ab12"})])

    def test_empty_recognition_not_sent(self):
        sess = FakeSession()
        rep = self._answerer(text="").handle(
            sess, C.from_interface(26, {"u": "https://x/b.png"}))
        self.assertFalse(rep["sent"])
        self.assertEqual(sess.sent, [])

    def test_same_url_not_answered_twice(self):
        sess = FakeSession()
        ans = self._answerer()
        ans.handle(sess, C.from_interface(26, {"u": "https://x/c.png"}))
        rep = ans.handle(sess, C.from_interface(26, {"u": "https://x/c.png"}))
        self.assertFalse(rep["sent"])
        self.assertEqual(len(sess.sent), 1)

    def test_dead_session_no_send(self):
        sess = FakeSession(connected=False)
        rep = self._answerer().handle(sess, C.from_interface(26, {"u": "https://x/d.png"}))
        self.assertFalse(rep["sent"])

    def test_dry_run_sends_nothing(self):
        sess = FakeSession()
        rep = self._answerer().handle(
            sess, C.from_interface(26, {"u": "https://x/e.png"}), dry_run=True)
        self.assertEqual(rep["answer"], "ab12")
        self.assertEqual(sess.sent, [])


class HandleCaptchaTests(unittest.TestCase):
    def test_not_captcha_returns_false(self):
        bot = FakeBot()
        self.assertFalse(C.handle_captcha(bot, 13, {"u": "https://x/1.png"},
                                          background=False))
        self.assertEqual(bot.session.sent, [])

    def test_captcha_without_url_returns_true_sends_nothing(self):
        bot = FakeBot()
        self.assertTrue(C.handle_captcha(bot, 26, {"o": 1}, background=False))
        self.assertEqual(bot.session.sent, [])

    def test_dead_session_does_not_crash(self):
        bot = FakeBot(FakeSession(connected=False))
        self.assertTrue(C.handle_captcha(bot, 26, {"u": "https://x/2.png"},
                                         background=False))


class DownloadTests(unittest.TestCase):
    def test_download_over_local_http(self):
        import captcha_solver as solver
        truth, img = solver.synth_captcha(text="ab12")
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "cap.png"), "wb") as f:
            f.write(img)

        # Без os.chdir: смена рабочего каталога ломает остальные тесты,
        # которые ищут файлы проекта относительными путями.
        def handler(*a, **k):
            return http.server.SimpleHTTPRequestHandler(*a, directory=tmp, **k)

        srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/cap.png" % srv.server_address[1]
            self.assertEqual(C.download_image(url, timeout=5), img)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
