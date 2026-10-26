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
KIND_IMAGE_URL = "image_url"

# Интерфейс, которым капча ходит в обе стороны.
# Снято с байткода BrNewCaptcha (classes4.dex, gui/legacy): getGuiId() -> 26,
# show() читает ключ "u", ответ уходит в тот же 26 с ключами "t"/"s".
CAPTCHA_INTERFACE = 26
KEY_IMAGE_URL = "u"
KEY_ANSWER_TYPE = "t"
KEY_ANSWER_TEXT = "s"
ANSWER_TYPE = 0

# Поле ввода в клиенте ограничено maxLength=20
# (res/layout/br_new_captcha.xml, br_new_caphca_line — опечатка в APK, так и есть).
FIELD_MAX_LEN = 20

MIN_LEN = 3
MAX_LEN = 8

_ANSWER_RE = re.compile(r"^[A-Za-z0-9]{%d,%d}$" % (MIN_LEN, MAX_LEN))
_ANSWER_RE_IMAGE = re.compile(r"^[A-Za-z0-9]{%d,%d}$" % (MIN_LEN, FIELD_MAX_LEN))
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
        image_url="",
        source="",
        interface_id=None,
        dialog_id=None,
        raw=None,
        received_at=None,
    ):
        self.kind = kind
        self.text = text
        self.image = image
        self.image_url = image_url
        self.source = source
        self.interface_id = interface_id
        self.dialog_id = dialog_id
        self.raw = raw or {}
        self.received_at = received_at if received_at is not None else time.time()
        self.answer = None
        self.solved_by = None
        # Заполняется решателем при неудаче. Раньше атрибут появлялся только
        # из SolverChain, и обращение к нему падало AttributeError.
        self.error = None

    def solved(self, answer, by):
        self.answer = answer
        self.solved_by = by

    def __repr__(self):
        return (
            f"CaptchaChallenge(kind={self.kind!r}, text={self.text!r}, "
            f"source={self.source!r}, iid={self.interface_id}, "
            f"dialog_id={self.dialog_id}, url={self.image_url!r}, "
            f"answer={self.answer!r})"
        )


def clean_text(value):
    """Убрать форматирование SA-MP и обрезать пробелы."""
    if not value:
        return ""
    text = _NOISE_RE.sub("", str(value))
    return text.strip()


def looks_like_answer(value, *, image=False):
    """Похоже ли значение на ответ капчи.

    Для капчи-картинки лимит другой: поле ввода в клиенте пускает до 20
    символов, а текст из пакета в BR короче. Раньше общий лимит в 8 символов
    молча отбрасывал верный ответ OCR.
    """
    text = clean_text(value)
    if not text or " " in text or "\n" in text:
        return False
    pattern = _ANSWER_RE_IMAGE if image else _ANSWER_RE
    return bool(pattern.match(text))


def looks_like_captcha_title(title):
    text = clean_text(title)
    return any(word in text for word in CAPTCHA_TITLES)


def _safe_int(value):
    """id диалога/интерфейса может прийти None — int(None) ронял шаг."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
                dialog_id=_safe_int(dialog_id),
                raw={"title": title, "info": info, "button1": button1, "button2": button2},
            )

    return CaptchaChallenge(
        KIND_MANUAL,
        source="dialog.unknown",
        dialog_id=_safe_int(dialog_id),
        raw={"title": title, "info": info, "button1": button1, "button2": button2},
    )


def from_interface(interface_id, payload):
    """Капча, пришедшая JSON-интерфейсом.

    Либо ответ прямо в payload, либо текстурная капча: payload несёт только
    id модели, а текст нарисован картинкой.

    Для интерфейса 26 (BrNewCaptcha) картинка лежит по URL в ключе "u" —
    клиент качает её через FirebasePerfUrlConnection.openStream().
    """
    if not isinstance(payload, dict):
        return None

    iid = _safe_int(interface_id)

    # Интерфейс 26 — единственный, где "u" означает URL картинки.
    # Проверяем до всего остального: у капчи-картинки в payload больше
    # ничего полезного нет.
    if iid == CAPTCHA_INTERFACE:
        url = payload.get(KEY_IMAGE_URL)
        if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://")):
            return CaptchaChallenge(
                KIND_IMAGE_URL,
                image_url=url.strip(),
                source="interface.u",
                interface_id=iid,
                raw=dict(payload),
            )
        # 26 без картинки — не наша капча, но и не повод лезть в общий разбор:
        # в 26 нет полей i/d/text/code, они относятся к другим интерфейсам.
        return None

    # 't' НЕ проверяем: в Black Russia это тип сообщения, а не текст ответа.
    # Раньше {'t':100} превращалось в капчу с ответом "100".
    for field in ("i", "d", "text", "code", "captcha"):
        value = payload.get(field)
        if isinstance(value, str) and looks_like_answer(value):
            return CaptchaChallenge(
                KIND_PACKET_TEXT,
                text=clean_text(value),
                source=f"interface.{field}",
                interface_id=iid,
                raw=dict(payload),
            )

    model_id = payload.get("m") or payload.get("model") or payload.get("model_id")
    if isinstance(model_id, int) and model_id > 0:
        return CaptchaChallenge(
            KIND_TEXTURE,
            source="interface.model",
            interface_id=iid,
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
        if challenge is None:
            return None
        errors = []
        for solver in self.solvers:
            try:
                answer = solver.solve(challenge)
            except Exception as exc:
                errors.append(f"{getattr(solver, 'name', '?')}: {exc!r}")
                continue
            if answer and looks_like_answer(
                answer, image=(challenge.kind == KIND_IMAGE_URL)
            ):
                challenge.solved(answer, getattr(solver, "name", solver.__class__.__name__))
                return answer
        if errors:
            # Молчание решателей раньше выглядело как «капча не решена», хотя
            # на деле они падали.
            challenge.error = "; ".join(errors)
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


# --- капча-картинка по URL (интерфейс 26) ----------------------------------
#
# Единственный рабочий формат, снятый с байткода BrNewCaptcha:
#
#   входящее : iid 26, ключ "u" -> URL картинки
#   ответ    : iid 26, {"t": 0, "s": "<текст>"}
#
# Оба отправителя в APK (кнопка «Готово» и onDismiss) шлют ровно это.
# Других вариантов в приложении нет — проверено по всем 7 dex.
#
# Ловушки:
#   * onDismiss шлёт ответ с ПУСТЫМ текстом, если поле не заполнено. Пустой
#     ответ засчитывается как неверный, поэтому мы его не отправляем.
#   * На одну и ту же картинку отвечаем один раз: повтор может считаться
#     второй попыткой.
#   * Кнопка «Готово» в клиенте неактивна (alpha 0.3), пока поле пустое,
#     и отправка идёт через 400 мс после клика. Мгновенный ответ — слишком
#     быстро для человека, античит такое ловит.


DOWNLOAD_TIMEOUT = 12.0
MAX_IMAGE_BYTES = 512 * 1024
ANSWER_DELAY = (0.6, 1.4)


def download_image(url, *, timeout=DOWNLOAD_TIMEOUT, user_agent=None):
    """Скачать картинку капчи.

    Клиент качает её обычным GET через FirebasePerfUrlConnection.openStream(),
    без особых заголовков — повторяем то же поведение.
    """
    import urllib.request

    if user_agent is None:
        try:
            from core.http_ping import generate_http_user_agent
            user_agent = generate_http_user_agent()
        except Exception:
            user_agent = "Dalvik/2.1.0 (Linux; U; Android 13)"

    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("картинка больше %d байт" % MAX_IMAGE_BYTES)
    if not data:
        raise ValueError("пустой ответ вместо картинки")
    return data


def solve_image(img_bytes, *, charset=None):
    """Распознать текст с картинки. Пустая строка — не удалось."""
    import captcha_solver

    kwargs = {}
    if charset:
        kwargs["charset"] = charset
    # Оценка шаблонов — не вероятность успеха. Не отправляем слабый
    # результат как достоверный ответ: нужен оператор или другой решатель.
    result = captcha_solver.solve(img_bytes, fast=False, **kwargs)
    confidence = float(result.get("confidence") or 0.0)
    if confidence < 0.80:
        raise ValueError(
            f"OCR требует проверки: text={result.get('text')!r}, "
            f"confidence={confidence:.4f}, method={result.get('method')}"
        )
    return str(result.get("text") or "").strip()[:FIELD_MAX_LEN]


class ImageUrlSolver:
    """Скачивает картинку по URL из капчи и распознаёт её.

    Встаёт в SolverChain последним: всё, что можно взять из пакета, должно
    быть взято до похода в сеть.
    """

    name = "ocr_url"

    def __init__(self, *, download=None, ocr=None, charset=None, log=None):
        self._download = download or download_image
        self._ocr = ocr or solve_image
        self.charset = charset
        self.log = log or (lambda msg: None)

    def solve(self, challenge):
        if challenge.kind != KIND_IMAGE_URL or not challenge.image_url:
            return None

        if not challenge.image:
            try:
                challenge.image = self._download(challenge.image_url)
            except Exception as exc:
                challenge.error = f"картинка не скачалась: {type(exc).__name__}: {exc}"
                self.log(f"[captcha] {challenge.error}")
                return None

        try:
            text = self._ocr(challenge.image, charset=self.charset)
        except Exception as exc:
            challenge.error = f"OCR: {type(exc).__name__}: {exc}"
            self.log(f"[captcha] {challenge.error}")
            return None

        if not text:
            self.log("[captcha] распознать не удалось, ответ не отправляем")
            return None

        return text


def answer_payload(answer):
    """Payload ответа на капчу. Единственный рабочий вариант."""
    return {KEY_ANSWER_TYPE: ANSWER_TYPE, KEY_ANSWER_TEXT: str(answer)}


def send_answer(session, answer, *, interface_id=CAPTCHA_INTERFACE):
    """Отправить ответ в интерфейс капчи."""
    return session.send_json(interface_id, answer_payload(answer))


class CaptchaAnswerer:
    """Решает капчу и отвечает. Один ответ на одну картинку."""

    def __init__(self, chain=None, *, delay=ANSWER_DELAY, log=None):
        self.chain = chain or default_chain()
        self.delay = delay
        self.log = log or (lambda msg: None)
        self._answered = {}
        self.last = None
        import threading
        self._lock = threading.RLock()
        self._inflight = set()
        self._current = {}

    def observe(self, session, challenge=None, *, closed=False):
        """Запомнить актуальную картинку до запуска фонового OCR."""
        with self._lock:
            if closed:
                self._current[id(session)] = None
            elif challenge is not None:
                self._current[id(session)] = challenge.image_url

    def _seen(self, url):
        now = time.time()
        for key in [k for k, t in self._answered.items() if now - t > 900]:
            self._answered.pop(key, None)
        return url in self._answered

    def handle(self, session, challenge, *, dry_run=False):
        """Решить и ответить. Возвращает отчёт для логов."""
        report = {"url": "", "answer": None, "sent": False,
                  "confirmed": False, "error": None}
        if challenge is None:
            report["error"] = "не капча"
            return report

        report["url"] = challenge.image_url
        if challenge.image_url and self._seen(challenge.image_url):
            report["error"] = "на эту картинку уже отвечали"
            return report

        # Решатели создаются раньше, чем становится известен лог бота.
        # Без этого их ошибки глохли, и провал выглядел как тишина.
        for solver in getattr(self.chain, "solvers", []):
            if hasattr(solver, "log"):
                solver.log = self.log

        answer = self.chain.solve(challenge)
        report["answer"] = answer
        if not answer:
            report["error"] = challenge.error or "не распознано"
            self.log(f"[captcha] ответа нет: {report['error']}")
            return report

        if dry_run:
            self.log(f"[captcha] сухой прогон: {answer!r}")
            return report

        if self.delay and self.delay[1] > 0:
            time.sleep((self.delay[0] + self.delay[1]) / 2.0)

        try:
            ok = send_answer(session, answer)
        except Exception as exc:
            report["error"] = f"отправка упала: {type(exc).__name__}: {exc}"
            self.log(f"[captcha] {report['error']}")
            return report

        report["sent"] = bool(ok)
        if ok:
            if challenge.image_url:
                self._answered[challenge.image_url] = time.time()
            self.log(f"[captcha] ответ отправлен: {answer!r}")
        else:
            report["error"] = "send_json вернул False"

        self.last = report
        return report


def default_chain(log=None):
    """Цепочка по умолчанию: сначала внешний сервис, потом локальный OCR.

    Локальный OCR ошибается на неоднозначных символах (l/1/i, V/v), а
    vision-модель читает те же картинки правильно. Оставляем его как
    запасной путь на случай, когда сервис недоступен.
    """
    solvers = []
    if os.environ.get("RAKOCR_DISABLE") != "1":
        try:
            from logic.captcha_api import RakOCRSolver

            solvers.append(RakOCRSolver(log=log))
        except Exception:
            pass
    solvers.append(ImageUrlSolver(log=log or (lambda msg: None)))
    return SolverChain(solvers)


_default_answerer = None


def get_answerer():
    global _default_answerer
    if _default_answerer is None:
        _default_answerer = CaptchaAnswerer()
    return _default_answerer


def handle_captcha(bot, interface_id, json_data, *, background=True):
    """Точка входа из обработчика JSON.

    Возвращает True, если пакет относился к капче (даже если ответ не ушёл).
    По умолчанию работает в фоновом потоке: скачивание и OCR занимают
    секунды, а обработчик пакетов блокировать нельзя.
    """
    if _safe_int(interface_id) != CAPTCHA_INTERFACE:
        return False

    challenge = from_interface(interface_id, json_data)
    session = getattr(bot, "session", None)

    def log(msg):
        try:
            if bot is not None and hasattr(bot, "add_log"):
                bot.add_log(msg.replace("[captcha]", "[КАПЧА]"))
        except Exception:
            pass

    if challenge is None:
        log(f"[captcha] iid 26 без картинки: {str(json_data)[:160]}")
        return True

    if session is None or not getattr(session, "connected", False):
        log("[captcha] сессия не подключена, ответить некому")
        return True

    answerer = get_answerer()
    answerer.log = log
    if background:
        import threading
        threading.Thread(
            target=answerer.handle, args=(session, challenge), daemon=True
        ).start()
    else:
        answerer.handle(session, challenge)
    return True
