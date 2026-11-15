"""Распознавание капчи через внешний сервис RakOCR.

Сервис принимает картинку и отдаёт готовый текст. Локальный OCR ошибается на
сложных капчах (путает l/1/i, V/v), а vision-модель читает их заметно лучше.

Формат, снятый с живого сервиса:

    POST /api/v1/solve
    Authorization: Bearer rko_...
    {"image_base64": "...", "mime_type": "image/png"}

    200 -> {"answer": "VaLA", "request_id": "..."}
    400 -> битая картинка
    401 -> ключ не расшифровался
    429 -> больше 30 запросов в минуту
    502 -> модель не смогла прочитать

Ответ может идти до минуты: модель сначала думает, потом отвечает.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from logic.captcha import (
    FIELD_MAX_LEN,
    KIND_IMAGE_URL,
    clean_text,
    looks_like_answer,
)

DEFAULT_API_URL = "https://rakocr.vercel.app/api/v1/solve"

# Ключ должен быть задан явно; встроенных учётных данных нет.
DEFAULT_API_KEY = ""

# Модель отвечает за 5-10 секунд, но запас нужен: 60 секунд названы как
# верхняя граница, плюс сеть.
SOLVE_TIMEOUT = 90.0
MAX_IMAGE_BYTES = 3_000_000


class RakOCRError(Exception):
    """Сервис ответил ошибкой. В тексте нет ключа."""


def detect_mime(data):
    """Тип картинки по сигнатуре. Сервис сверяет его с байтами."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _post(url, key, payload, timeout):
    """Один запрос к сервису. Возвращает dict ответа."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error") or ""
        except Exception:
            pass
        raise RakOCRError(f"HTTP {exc.code}: {detail or 'без описания'}") from None
    except urllib.error.URLError as exc:
        raise RakOCRError(f"сеть: {exc.reason}") from None
    except TimeoutError:
        raise RakOCRError("таймаут") from None

    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RakOCRError("ответ не JSON") from None


def solve_bytes(image, *, api_key=None, api_url=None, timeout=SOLVE_TIMEOUT,
                retries=1, log=None):
    """Отправить картинку в сервис и вернуть текст. None — не распознано."""
    log = log or (lambda msg: None)

    if not image:
        return None
    if len(image) > MAX_IMAGE_BYTES:
        log("[rakocr] картинка больше %d байт" % MAX_IMAGE_BYTES)
        return None

    mime = detect_mime(image)
    if mime is None:
        log("[rakocr] неизвестный формат картинки")
        return None

    key = api_key or os.environ.get("RAKOCR_KEY") or DEFAULT_API_KEY
    url = api_url or os.environ.get("RAKOCR_URL") or DEFAULT_API_URL
    if not key:
        log("[rakocr] ключ не указан")
        return None

    payload = {
        "image_base64": base64.b64encode(image).decode("ascii"),
        "mime_type": mime,
    }

    last_error = None
    for attempt in range(retries + 1):
        started = time.time()
        try:
            data = _post(url, key, payload, timeout)
        except RakOCRError as exc:
            last_error = str(exc)
            # Повторяем только то, что имеет смысл: таймаут и 5xx.
            retryable = "таймаут" in last_error or "сеть" in last_error or "HTTP 5" in last_error
            log(f"[rakocr] попытка {attempt + 1}: {last_error}")
            if not retryable or attempt == retries:
                break
            continue

        answer = clean_text(str(data.get("answer") or ""))
        elapsed = time.time() - started
        # "UNREADABLE" — служебный маркер модели, а не ответ. Формально он
        # проходит looks_like_answer, поэтому отсекаем отдельно.
        if answer.upper() == "UNREADABLE":
            answer = ""
        if answer and looks_like_answer(answer, image=True):
            log(f"[rakocr] {answer!r} за {elapsed:.1f}с")
            return answer[:FIELD_MAX_LEN]

        last_error = f"сервис вернул нечитаемое: {data.get('answer')!r}"
        log(f"[rakocr] {last_error}")
        break

    if last_error:
        log(f"[rakocr] ответа нет: {last_error}")
    return None


class RakOCRSolver:
    """Решатель для SolverChain: скачивает картинку и отдаёт её сервису."""

    name = "rakocr"

    def __init__(self, *, api_key=None, api_url=None, timeout=SOLVE_TIMEOUT,
                 retries=1, log=None):
        self.api_key = api_key
        self.api_url = api_url
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.log = log or (lambda msg: None)

    def solve(self, challenge):
        if challenge is None or challenge.kind != KIND_IMAGE_URL:
            return None

        image = challenge.image
        if not image:
            try:
                from logic.captcha import download_image
                image = download_image(challenge.image_url)
            except Exception as exc:
                challenge.error = f"картинка не скачалась: {type(exc).__name__}: {exc}"
                self.log(f"[rakocr] {challenge.error}")
                return None
            challenge.image = image

        return solve_bytes(
            image,
            api_key=self.api_key,
            api_url=self.api_url,
            timeout=self.timeout,
            retries=self.retries,
            log=self.log,
        )


def _main(argv):
    """CLI: python3 -m logic.captcha_api captcha.png"""
    import sys

    if len(argv) < 2:
        print("usage: python3 -m logic.captcha_api <image.png> [--url URL] [--key KEY]")
        return 2

    path = argv[1]
    api_url = None
    api_key = None
    rest = argv[2:]
    while rest:
        flag = rest.pop(0)
        if flag == "--url" and rest:
            api_url = rest.pop(0)
        elif flag == "--key" and rest:
            api_key = rest.pop(0)

    with open(path, "rb") as fh:
        image = fh.read()

    answer = solve_bytes(image, api_key=api_key, api_url=api_url,
                         log=lambda msg: print(msg, file=sys.stderr))
    print(answer if answer else "не распознано")
    return 0 if answer else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
