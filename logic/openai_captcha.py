"""Vision captcha recognition using an OpenAI-compatible Chat Completions API."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from urllib.parse import urlsplit

from logic.captcha import KIND_IMAGE_URL, FIELD_MAX_LEN, clean_text, looks_like_answer
from logic.captcha_api import detect_mime, MAX_IMAGE_BYTES

PROMPT = ("Read only the letters and digits shown in the image. "
          "Preserve uppercase and lowercase. Return only the exact text, "
          "without explanations or formatting. If unreadable, return UNREADABLE.")


def settings():
    """Read dynamically; shell environment takes precedence over project .env."""
    try:
        from dotenv import dotenv_values
        values = dotenv_values(Path(__file__).resolve().parent.parent / '.env')
    except ImportError:
        values = {}
    def get(key):
        return (os.environ.get(key, values.get(key) or '') or '').strip()
    return get('CAPTCHA_BASE_URL'), get('CAPTCHA_API_TOKEN'), get('CAPTCHA_MODEL')


def solve_bytes(image, *, base_url=None, api_token=None, model=None, log=None, client_factory=None):
    log = log or (lambda message: None)
    if not image or len(image) > MAX_IMAGE_BYTES:
        log('[captcha-ai] Изображение пустое или превышает лимит 3 МБ')
        return None
    mime = detect_mime(image)
    if not mime:
        log('[captcha-ai] Допустимы PNG, JPEG и WebP')
        return None
    env_url, env_token, env_model = settings()
    base_url = env_url if base_url is None else base_url.strip()
    api_token = env_token if api_token is None else api_token.strip()
    model = env_model if model is None else model.strip()
    parts = urlsplit(base_url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        log('[captcha-ai] Укажите HTTPS Base URL без учётных данных и параметров')
        return None
    if not api_token or not model:
        log('[captcha-ai] Укажите CAPTCHA_API_TOKEN и CAPTCHA_MODEL')
        return None
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        with client_factory(base_url=base_url, api_key=api_token, timeout=60.0, max_retries=1) as client:
            result = client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': [
                    {'type': 'text', 'text': PROMPT},
                    {'type': 'image_url', 'image_url': {
                        'url': f'data:{mime};base64,{base64.b64encode(image).decode("ascii")}'}}
                ]}],
            )
        if not result.choices:
            log('[captcha-ai] Провайдер вернул пустой ответ')
            return None
        message = result.choices[0].message
        if getattr(message, 'refusal', None) or not isinstance(message.content, str):
            log('[captcha-ai] Модель отказала или не вернула текст')
            return None
        answer = clean_text(message.content).strip('`\"\' ')
        if answer.upper() == 'UNREADABLE' or not looks_like_answer(answer, image=True):
            log('[captcha-ai] Модель вернула нераспознанный или некорректный ответ')
            return None
        return answer[:FIELD_MAX_LEN]
    except ImportError:
        log('[captcha-ai] Не установлен OpenAI SDK: pip install -r requirements.txt')
    except Exception as exc:
        # Provider exceptions can contain a request, token, URL, or response body.
        status = getattr(exc, 'status_code', None)
        log(f'[captcha-ai] Ошибка провайдера: {type(exc).__name__}' + (f' (HTTP {status})' if isinstance(status, int) else ''))
    return None


class OpenAICaptchaSolver:
    name = 'openai_vision'

    def __init__(self, *, log=None):
        self.log = log or (lambda message: None)

    def solve(self, challenge):
        if challenge is None or challenge.kind != KIND_IMAGE_URL:
            return None
        image = challenge.image
        if not image:
            try:
                from logic.captcha import download_image
                image = download_image(challenge.image_url)
            except Exception as exc:
                challenge.error = f'Не удалось скачать изображение: {type(exc).__name__}'
                self.log(f'[captcha-ai] {challenge.error}')
                return None
            challenge.image = image
        answer = solve_bytes(image, log=self.log)
        if not answer:
            challenge.error = 'ИИ не вернул подходящий ответ; проверьте настройки и журнал'
        return answer
