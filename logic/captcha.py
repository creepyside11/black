"""Схема решения капчи Black Russia.

Капча приходит в трёх видах, и решаются они по-разному:

  1. packet_text   — текст ответа лежит прямо в пакете (textdraw/dialog/interface).
                     Решается мгновенно, без OCR и без картинок. Это основной путь
                     в CRMP: сервер присылает строку, клиент рисует её шрифтом.

  2. texture_image — ответ запечён в текстуру (model_id != 0, text пустой).
                     Из пакетов текст не достаётся — нужен кадр или сам .txd.
                     Сюда идёт OCR, если картинку удалось собрать.

  3. manual        — ничего не распознали. Капча уходит оператору через файл,
                     ответ возвращается в игру.

Порядок важен: сначала дешёвые пути, OCR последним.
"""

from __future__ import annotations

import os
import re
import time

KIND_PACKET_TEXT = "packet_text"
KIND_TEXTURE = "texture_image"
KIND_MANUAL = "manual"

MIN_LEN = 3
MAX_LEN = 8

_ANSWER_RE = re.compile(r"^[A-Za-z0-9]{%d,%d}$" % (MIN_LEN, MAX_LEN))
_NOISE_RE = re.compile(r"\{[0-9A-Fa-f]{6}\}|~[a-zA-Z]~|_[A-Z]")

CAPTCHA_TITLES = (
    "ПРОВЕРКА",
    "Проверка",
    "КАПЧА",
    "Капча",
    "Подтверждение",
)


class CaptchaChallenge:
    """Одна капча, какой бы вид она ни приняла."""

    def __init__(
        self,
        kind,
        *,
        text="",
        image=b"",
        source="",
        interface_id=None,
        dialog_id=None,
        raw=None,
        received_at=None,
    ):
        self.kind = kind
        self.text = text
        self.image = image
        self.source = source
        self.interface_id = interface_id
        self.dialog_id = dialog_id
        self.raw = raw or {}
        self.received_at = received_at if received_at is not None else time.time()
        self.answer = None
        self.solved_by = None

    def solved(self, answer, by):
        self.answer = answer
        self.solved_by = by

    def __repr__(self):
        return (
            f"CaptchaChallenge(kind={self.kind!r}, text={self.text!r}, "
            f"source={self.source!r}, iid={self.interface_id}, "
            f"dialog_id={self.dialog_id}, answer={self.answer!r})"
        )


def clean_text(value):
    """Убрать форматирование SA-MP и обрезать пробелы."""
    if not value:
        return ""
    text = _NOISE_RE.sub("", str(value))
    return text.strip()


def looks_like_answer(value):
    """Похоже ли значение на ответ капчи."""
    text = clean_text(value)
    if not text or " " in text or "\n" in text:
        return False
    return bool(_ANSWER_RE.match(text))


def looks_like_captcha_title(title):
    text = clean_text(title)
    return any(word in text for word in CAPTCHA_TITLES)


def from_dialog(dialog_id, title, info, button1="", button2=""):
    """Капча, пришедшая обычным SA-MP диалогом.

    Ответ может лежать в title, в info или в тексте кнопок — серверы кладут
    по-разному. Проверяем все поля, первое похожее на ответ и берём.
    """
    if not looks_like_captcha_title(title) and not looks_like_captcha_title(info):
        return None

    for field, value in (
        ("title", title),
        ("info", info),
        ("button1", button1),
        ("button2", button2),
    ):
        cleaned = clean_text(value)
        if looks_like_answer(cleaned):
            return CaptchaChallenge(
                KIND_PACKET_TEXT,
                text=cleaned,
                source=f"dialog.{field}",
                dialog_id=int(dialog_id),
                raw={"title": title, "info": info, "button1": button1, "button2": button2},
            )

    return CaptchaChallenge(
        KIND_MANUAL,
        source="dialog.unknown",
        dialog_id=int(dialog_id),
        raw={"title": title, "info": info, "button1": button1, "button2": button2},
    )


def from_interface(interface_id, payload):
    """Капча, пришедшая JSON-интерфейсом.

    Либо ответ прямо в payload, либо текстурная капча: payload несёт только
    id модели, а текст нарисован картинкой.
    """
    if not isinstance(payload, dict):
        return None

    for field in ("t", "i", "d", "text", "code", "captcha"):
        value = payload.get(field)
        if looks_like_answer(value):
            return CaptchaChallenge(
                KIND_PACKET_TEXT,
                text=clean_text(value),
                source=f"interface.{field}",
                interface_id=int(interface_id),
                raw=dict(payload),
            )

    model_id = payload.get("m") or payload.get("model") or payload.get("model_id")
    if isinstance(model_id, int) and model_id > 0:
        return CaptchaChallenge(
            KIND_TEXTURE,
            source="interface.model",
            interface_id=int(interface_id),
            raw=dict(payload),
        )

    return None


def from_text_draw(draw):
    """Капча в виде textdraw. Текст может быть, а может быть запечён в модель."""
    if not isinstance(draw, dict):
        return None

    text = clean_text(draw.get("text"))
    model_id = int(draw.get("model_id") or 0)

    if looks_like_answer(text):
        return CaptchaChallenge(
            KIND_PACKET_TEXT, text=text, source="textdraw.text", raw=dict(draw)
        )

    if model_id > 0 and not text:
        return CaptchaChallenge(KIND_TEXTURE, source="textdraw.model", raw=dict(draw))

    return None


class SolverChain:
    """Пробует решатели по очереди, пока один не даст ответ."""

    def __init__(self, solvers=None):
        self.solvers = list(solvers or [])

    def add(self, solver):
        self.solvers.append(solver)

    def solve(self, challenge):
        for solver in self.solvers:
            try:
                answer = solver.solve(challenge)
            except Exception:
                continue
            if answer and looks_like_answer(answer):
                challenge.solved(answer, getattr(solver, "name", solver.__class__.__name__))
                return answer
        return None


class PacketTextSolver:
    """Ответ уже в пакете — просто берём его."""

    name = "packet_text"

    def solve(self, challenge):
        if challenge.kind != KIND_PACKET_TEXT:
            return None
        return challenge.text or None


class RelaySolver:
    """Отдать капчу оператору через файл и дождаться ответа.

    Пишем запрос в request_path, ждём answer_path. Аварийный путь:
    работает всегда, но требует человека.
    """

    name = "relay"

    def __init__(self, request_path, answer_path, *, timeout=180.0, log=None):
        self.request_path = request_path
        self.answer_path = answer_path
        self.timeout = float(timeout)
        self.log = log or (lambda msg: None)

    def solve(self, challenge):
        if os.path.exists(self.answer_path):
            os.remove(self.answer_path)

        lines = [
            f"kind={challenge.kind}",
            f"source={challenge.source}",
            f"iid={challenge.interface_id}",
            f"dialog_id={challenge.dialog_id}",
            f"text={challenge.text}",
            f"raw={challenge.raw}",
        ]
        with open(self.request_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        self.log(f"[captcha] нужен ответ оператора -> {self.request_path}")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(1.0)
            if not os.path.exists(self.answer_path):
                continue
            try:
                with open(self.answer_path, encoding="utf-8") as fh:
                    answer = clean_text(fh.read())
            except OSError:
                continue
            try:
                os.remove(self.answer_path)
            except OSError:
                pass
            if looks_like_answer(answer):
                self.log(f"[captcha] оператор дал ответ: {answer}")
                return answer
        self.log("[captcha] оператор не ответил за отведённое время")
        return None
