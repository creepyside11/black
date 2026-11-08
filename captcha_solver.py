#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
captcha_solver.py — распознавание капчи Black Russia (CRMP) на Termux.

Что подтверждено дизасмом клиента (classes4.dex, BrNewCaptcha):

    входящий JSON с ключом "u"  ->  URL картинки
    screenId 26
    URL -> FirebasePerfUrlConnection.openStream() -> BitmapFactory.decodeStream()
    ответ: JSON {"t":0,"s":"<текст>"} -> GUIManager.sendJsonData(26, json)

Модуль импортируется и работает без numpy / cv2 / pytesseract / tesseract.
Всё, что нужно — Pillow. tesseract CLI подхватывается автоматически, если он
есть в PATH.

Публичный API:
    preprocess(img, ...)              -> PIL.Image  (L, бинарная, апскейл x2)
    segment(img, ...)                 -> list[box]  (x0, y0, x1, y1)
    ocr_tesseract(img, ...)           -> dict
    ocr_template(img, templates, ...) -> dict
    solve(img_bytes, ...)             -> dict  (лучший кандидат + уверенность)

Дополнительно:
    build_templates(), save_templates(), load_templates(), ensure_templates()
    learn(text, img_bytes)            — дообучить банк шаблонов реальной капчей
    synth_captcha()                   — генератор синтетики для тестов
    bench()                           — прогон точности
"""

from __future__ import annotations

import io
import json
import math
import os
import pickle
import random
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    ImageStat,
)

__version__ = "1.0"

# Цифры + строчная латиница — типовая капча CRMP-подобных клиентов.
# Если в реальной капче есть заглавные/спецсимволы, передай свой charset.
DEFAULT_CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"
DEFAULT_CHARSET_EXT = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE_PATH = os.path.join(MODULE_DIR, "captcha_templates.pkl")
TMP_DIR = os.path.join(MODULE_DIR, ".tess_tmp")

# --- внешние зависимости: все опциональные -------------------------------

TESSERACT_BIN = shutil.which("tesseract")

try:  # pytesseract не обязателен
    import pytesseract  # type: ignore

    HAS_PYTESSERACT = True
except Exception:  # pragma: no cover - зависит от системы
    pytesseract = None  # type: ignore
    HAS_PYTESSERACT = False

try:  # numpy/cv2 только для информации, код на них не опирается
    import numpy  # type: ignore

    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False

try:
    import cv2  # type: ignore

    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

HAS_TESSERACT = bool(TESSERACT_BIN) or HAS_PYTESSERACT

# Шрифты, из которых по умолчанию строится банк шаблонов.
DEFAULT_TEMPLATE_FONTS = [
    "/system/fonts/Roboto-Regular.ttf",
    "/system/fonts/DroidSans.ttf",
    "/system/fonts/DroidSans-Bold.ttf",
    "/system/fonts/DroidSansMono.ttf",
    "/system/fonts/CutiveMono.ttf",
    "/system/fonts/ComingSoon.ttf",
    "/system/fonts/CarroisGothicSC-Regular.ttf",
]

# Шрифты, которых в банке нет — на них проверяется обобщение.
HELDOUT_FONTS = [
    "/system/fonts/DancingScript-Regular.ttf",
    "/system/fonts/SourceSansPro-Regular.ttf",
    "/system/fonts/NotoSerif-Regular.ttf",
]


# =========================================================================
# базовые утилиты
# =========================================================================

def _load(src):
    """Принимает bytes / путь / PIL.Image, возвращает PIL.Image."""
    if isinstance(src, Image.Image):
        return src
    if isinstance(src, (bytes, bytearray, memoryview)):
        return Image.open(io.BytesIO(bytes(src)))
    return Image.open(src)


def _to_gray(img):
    img = _load(img)
    if img.mode == "L":
        return img.copy()
    if img.mode in ("RGBA", "LA", "P"):
        # прозрачный фон -> белый, иначе альфа даёт чёрные края
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        bg.alpha_composite(rgba)
        return bg.convert("L")
    return img.convert("L")


def _bin_table(thr):
    """Таблица для point(): <=thr -> 0 (чернила), иначе 255."""
    return [0 if i <= thr else 255 for i in range(256)]


def otsu_threshold(gray):
    """Порог Оцу по гистограмме. Чистый Python, без numpy."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best_var = -1.0
    thr = 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var = var
            thr = t
    return thr


def _ink_fraction(binary):
    total = binary.width * binary.height
    if not total:
        return 0.0
    return binary.histogram()[0] / total


def _mean_luma(gray):
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 128.0
    return sum(i * h for i, h in enumerate(hist)) / total


# =========================================================================
# 1. preprocess
# =========================================================================

def strip_long_lines(binimg, run_ratio=0.85):
    """Убирает сплошные горизонтальные/вертикальные линии.

    Линия отличается от текста тем, что даёт непрерывный пробег чернил почти
    на всю ширину (высоту). У строки текста максимальный непрерывный пробег
    равен ширине одного символа, то есть ~15-20% — порог 0.85 их не задевает.
    """
    w, h = binimg.size
    px = binimg.load()
    rows = []
    cols = []

    for y in range(h):
        best = cur = 0
        for x in range(w):
            if px[x, y] == 0:
                cur += 1
                if cur > best:
                    best = cur
            else:
                cur = 0
        if best >= run_ratio * w:
            rows.append(y)

    for x in range(w):
        best = cur = 0
        for y in range(h):
            if px[x, y] == 0:
                cur += 1
                if cur > best:
                    best = cur
            else:
                cur = 0
        if best >= run_ratio * h:
            cols.append(x)

    if not rows and not cols:
        return binimg

    out = binimg.copy()
    op = out.load()
    for y in rows:
        for x in range(w):
            op[x, y] = 255
    for x in cols:
        for y in range(h):
            op[x, y] = 255
    return out


def despeckle(binimg, min_px=8):
    """Удаляет связные компоненты меньше min_px пикселей (8-связность)."""
    w, h = binimg.size
    px = binimg.load()
    seen = bytearray(w * h)
    out = binimg.copy()
    op = out.load()
    neigh = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))

    for y0 in range(h):
        base = y0 * w
        for x0 in range(w):
            if seen[base + x0] or px[x0, y0] != 0:
                continue
            stack = [(x0, y0)]
            seen[base + x0] = 1
            comp = []
            while stack:
                x, y = stack.pop()
                comp.append((x, y))
                for dx, dy in neigh:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if not seen[j] and px[nx, ny] == 0:
                            seen[j] = 1
                            stack.append((nx, ny))
            if len(comp) < min_px:
                for x, y in comp:
                    op[x, y] = 255
    return out


def morph_open(binimg, size=3):
    """Открытие по чернилам: убирает структуры тоньше size пикселей.

    MaxFilter расширяет белый фон (чернила худеют), MinFilter возвращает
    их обратно. Всё, что тоньше size, исчезает — это тонкие линии помех и
    шумовые штрихи. Штрихи символов после апскейла x2 заметно толще.
    """
    return binimg.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))


def preprocess(img, scale=2.0, denoise=True, strip_lines=True,
               threshold=None, invert="auto", contrast=1.6, cutoff=1,
               morph=3):
    """Готовит капчу к распознаванию.

    grayscale -> авто-инверсия тёмного фона -> autocontrast + контраст ->
    апскейл x2 (LANCZOS) -> медианный фильтр 3x3 -> бинаризация Оцу ->
    морфологическое открытие -> удаление длинных линий -> удаление мелких
    компонент.

    morph: размер ядра открытия. 3 сохраняет тонкие штрихи, 5 снимает линии
    помех толщиной до ~4 px (после апскейла). solve() считает оба варианта.

    Возвращает PIL.Image mode "L": чернила = 0, фон = 255.
    """
    gray = _to_gray(img)

    if invert == "auto":
        if _mean_luma(gray) < 110:
            gray = ImageOps.invert(gray)
    elif invert:
        gray = ImageOps.invert(gray)

    gray = ImageOps.autocontrast(gray, cutoff=cutoff)
    if contrast and contrast != 1.0:
        gray = ImageEnhance.Contrast(gray).enhance(contrast)

    if scale and scale != 1.0:
        nw = max(1, int(round(gray.width * scale)))
        nh = max(1, int(round(gray.height * scale)))
        gray = gray.resize((nw, nh), Image.LANCZOS)

    if denoise:
        gray = gray.filter(ImageFilter.MedianFilter(3))

    thr = int(threshold) if threshold is not None else otsu_threshold(gray)
    binary = gray.point(_bin_table(thr), mode="L")

    # на всякий случай: если «чернил» больше половины — картинка инвертирована
    if _ink_fraction(binary) > 0.55:
        binary = ImageOps.invert(binary)

    if morph:
        binary = morph_open(binary, morph)

    if strip_lines:
        binary = strip_long_lines(binary)

    if denoise:
        min_px = max(6, int(0.0004 * binary.width * binary.height))
        binary = despeckle(binary, min_px=min_px)

    return binary


# =========================================================================
# 2. segment
# =========================================================================

def _column_projection(binimg):
    w, h = binimg.size
    px = binimg.load()
    proj = [0] * w
    for x in range(w):
        c = 0
        for y in range(h):
            if px[x, y] == 0:
                c += 1
        proj[x] = c
    return proj


def _box_vertical_extent(binimg, x0, x1):
    w, h = binimg.size
    px = binimg.load()
    x1 = min(x1, w)
    top = None
    bot = None
    for y in range(h):
        hit = False
        for x in range(x0, x1):
            if px[x, y] == 0:
                hit = True
                break
        if hit:
            if top is None:
                top = y
            bot = y
    if top is None:
        return None
    return top, bot + 1


def _split_box(binimg, box, proj, min_gap=1):
    """Режет слишком широкий бокс по локальному минимуму проекции."""
    x0, y0, x1, y1 = box
    span = x1 - x0
    if span < 6:
        return [box]
    lo = x0 + int(span * 0.25)
    hi = x0 + int(span * 0.75)
    if hi <= lo:
        return [box]
    seg = proj[lo:hi]
    if not seg:
        return [box]
    mn = min(seg)
    peak = max(proj[x0:x1]) or 1
    if mn > 0.20 * peak:
        return [box]  # нет выраженной впадины — символ слитный
    cut = lo + seg.index(mn)
    if cut - x0 < 3 or x1 - cut < 3:
        return [box]
    left = [x0, y0, cut, y1]
    right = [cut, y0, x1, y1]
    return [left, right]


def segment(img, min_height_ratio=0.30, gap_thr_ratio=0.03, merge_gap=1,
            split_wide=True, min_width=2, expand=2, baseline_pct=0.05):
    """Нарезка на символы по вертикальной проекции.

    img — бинарная L (чернила = 0). Возвращает список боксов
    [x0, y0, x1, y1] слева направо.

    Порог проекции берётся как доля высоты (по умолчанию 3%), а не как
    «хоть один пиксель»: иначе тонкая линия помех или шумовая точка в
    каждом столбце склеивают всю картинку в один бокс. После нахождения
    ядра символа границы расширяются на expand пикселей, чтобы не срезать
    тонкие штрихи по краям.

    baseline_pct — низкий перцентиль проекции; если он больше нуля, это
    фон от линий помех, и он вычитается. На чистой картинке перцентиль
    равен нулю и ничего не меняется.
    """
    if img.mode != "L":
        img = _to_gray(img)
    w, h = img.size
    if not w or not h:
        return []

    proj = _column_projection(img)

    # Линия помех добавляет почти одинаковую добавку в каждый столбец, из-за
    # чего «пустых» столбцов не остаётся и все символы склеиваются. Оцениваем
    # этот фон низким перцентилем проекции и вычитаем его. На чистой картинке
    # перцентиль равен нулю и ничего не меняется.
    if proj and baseline_pct:
        ordered = sorted(proj)
        base = ordered[min(len(ordered) - 1, int(len(ordered) * baseline_pct))]
        if base > 0:
            proj = [max(0, v - base) for v in proj]

    thr = max(2, int(gap_thr_ratio * h))

    runs = []
    start = None
    for x, v in enumerate(proj):
        if v > thr:
            if start is None:
                start = x
        else:
            if start is not None:
                runs.append([start, x])
                start = None
    if start is not None:
        runs.append([start, w])

    if not runs:
        return []

    # расширяем ядра на соседние столбцы с любыми чернилами (не дальше expand)
    if expand:
        for r in runs:
            x0, x1 = r
            steps = 0
            while x0 - 1 >= 0 and proj[x0 - 1] > 0 and steps < expand:
                x0 -= 1
                steps += 1
            steps = 0
            while x1 < w and proj[x1] > 0 and steps < expand:
                x1 += 1
                steps += 1
            r[0], r[1] = x0, x1

    # склеиваем разрывы в 1-2 пикселя (артефакты JPEG/сглаживания)
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    runs = [r for r in merged if r[1] - r[0] >= min_width]

    boxes = []
    for x0, x1 in runs:
        ext = _box_vertical_extent(img, x0, x1)
        if ext is None:
            continue
        top, bot = ext
        boxes.append([x0, top, x1, bot])

    if not boxes:
        return []

    max_h = max(b[3] - b[1] for b in boxes)
    boxes = [b for b in boxes if (b[3] - b[1]) >= min_height_ratio * max_h]

    if not boxes:
        return []

    if split_wide and len(boxes) > 1:
        widths = sorted(b[2] - b[0] for b in boxes)
        median_w = widths[len(widths) // 2]
        out = []
        for b in boxes:
            if (b[2] - b[0]) > 2.0 * median_w:
                out.extend(_split_box(img, b, proj))
            else:
                out.append(b)
        boxes = out

    boxes.sort(key=lambda b: b[0])
    return boxes


def crop_segments(img, boxes):
    return [img.crop((b[0], b[1], b[2], b[3])) for b in boxes]


# =========================================================================
# 3. ocr_tesseract
# =========================================================================

def _tmp_png(img):
    os.makedirs(TMP_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".png", dir=TMP_DIR)
    os.close(fd)
    img.save(path, "PNG")
    return path


def _clean_text(text, charset):
    return "".join(c for c in text if c in charset)


def _tess_tsv(path, psm, whitelist, lang):
    """Один прогон tesseract в TSV: текст + средняя уверенность по словам."""
    cmd = [TESSERACT_BIN, path, "stdout", "-l", lang, "--psm", str(psm), "tsv"]
    if whitelist:
        cmd += ["-c", "tessedit_char_whitelist=" + whitelist]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return "", 0.0, (proc.stderr or "").strip()[:200]

    words = []
    confs = []
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        word = cols[11].strip()
        if not word:
            continue
        words.append(word)
        try:
            c = float(cols[10])
        except ValueError:
            c = -1.0
        if c >= 0:
            confs.append(c)

    text = "".join(words)
    if confs:
        conf = sum(confs) / len(confs) / 100.0
    else:
        conf = 0.55 if text else 0.0
    return text, conf, ""


def _tess_pytesseract(img, psm, whitelist, lang):
    cfg = "--psm {} -c tessedit_char_whitelist={}".format(psm, whitelist)
    data = pytesseract.image_to_data(img, lang=lang, config=cfg,
                                     output_type=pytesseract.Output.DICT)
    words = []
    confs = []
    for i, txt in enumerate(data.get("text", [])):
        txt = (txt or "").strip()
        if not txt:
            continue
        words.append(txt)
        c = float(data["conf"][i])
        if c >= 0:
            confs.append(c)
    text = "".join(words)
    conf = (sum(confs) / len(confs) / 100.0) if confs else (0.55 if text else 0.0)
    return text, conf, ""


def ocr_tesseract(img, psm=(8, 7), charset=DEFAULT_CHARSET, lang="eng",
                  whitelist=None, timeout=30, expected_len=None):
    """Распознавание через tesseract (pytesseract или CLI).

    Возвращает dict:
        text, conf, method, available, raw (список прогонов по psm)
    Если tesseract недоступен — available=False, text="", conf=0.0.
    """
    result = {
        "text": "",
        "conf": 0.0,
        "method": "tesseract",
        "available": HAS_TESSERACT,
        "raw": [],
    }
    if not HAS_TESSERACT:
        return result

    wl = whitelist if whitelist is not None else charset
    work = _to_gray(img)

    # мелкий текст tesseract читает плохо: гарантируем высоту хотя бы ~40px
    if work.height < 40:
        k = 40.0 / work.height
        work = work.resize((max(1, int(work.width * k)), 40), Image.LANCZOS)
    work = work.convert("L")

    raw = []
    if HAS_PYTESSERACT:
        try:
            for p in psm:
                text, conf, err = _tess_pytesseract(work, p, wl, lang)
                raw.append({"psm": p, "text": text, "conf": conf, "err": err})
                if expected_len and len(_clean_text(text, charset)) == expected_len and conf >= 0.4:
                    break
        except Exception as exc:  # noqa: BLE001
            raw.append({"psm": psm[0], "text": "", "conf": 0.0, "err": str(exc)})
    elif TESSERACT_BIN:
        path = _tmp_png(work)
        try:
            for p in psm:
                text, conf, err = _tess_tsv(path, p, wl, lang)
                raw.append({"psm": p, "text": text, "conf": conf, "err": err})
                if expected_len and len(_clean_text(text, charset)) == expected_len and conf >= 0.4:
                    break
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    for r in raw:
        r["text"] = _clean_text(r.get("text", ""), charset)

    best = None
    for r in raw:
        if not r["text"]:
            continue
        key = (r["conf"], len(r["text"]))
        if best is None or key > (best["conf"], len(best["text"])):
            best = r

    result["raw"] = raw
    if best:
        result["text"] = best["text"]
        result["conf"] = best["conf"]
        result["psm"] = best["psm"]
    return result


# =========================================================================
# 4. ocr_template
# =========================================================================

GLYPH_SIZE = (28, 28)


def normalize_glyph(img, size=GLYPH_SIZE, pad=2):
    """Обрезает по чернилам и вписывает глиф в size, сохраняя пропорции."""
    img = _to_gray(img)
    ink_bbox = ImageOps.invert(img).getbbox()
    if ink_bbox:
        img = img.crop(ink_bbox)
    w, h = img.size
    if w == 0 or h == 0:
        return Image.new("L", size, 255)

    tw = max(1, size[0] - 2 * pad)
    th = max(1, size[1] - 2 * pad)
    k = min(tw / float(w), th / float(h))
    nw = max(1, int(round(w * k)))
    nh = max(1, int(round(h * k)))
    img = img.resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("L", size, 255)
    canvas.paste(img, ((size[0] - nw) // 2, (size[1] - nh) // 2))
    return canvas.point(_bin_table(127), mode="L")


def _to_bin1(img):
    return img.convert("1", dither=Image.Dither.NONE)


def _similarity(a, b, metric="sad", a1=None, b1=None):
    """Сходство двух нормализованных глифов, 0..1 (больше = похожее)."""
    if metric == "iou":
        x = a1 if a1 is not None else _to_bin1(a)
        y = b1 if b1 is not None else _to_bin1(b)
        inter = ImageChops.logical_and(x, y).histogram()[255]
        union = ImageChops.logical_or(x, y).histogram()[255]
        return (inter / float(union)) if union else 0.0

    if metric == "cos":
        dot = ImageStat.Stat(ImageChops.multiply(a, b)).sum[0]
        na = ImageStat.Stat(ImageChops.multiply(a, a)).sum[0]
        nb = ImageStat.Stat(ImageChops.multiply(b, b)).sum[0]
        if not na or not nb:
            return 0.0
        return dot / math.sqrt(na * nb)

    # sum of absolute differences, нормированная в 0..1
    diff = ImageStat.Stat(ImageChops.difference(a, b)).sum[0]
    return 1.0 - diff / (255.0 * a.width * a.height)


def ocr_template(img, templates, metric="sad", size=None, charset=None,
                 boxes=None):
    """Распознавание по шаблонам.

    templates: dict {символ: [PIL.Image, ...]} нормализованных глифов.
    metric: "sad" | "iou" | "cos".

    Уверенность: 0.75 веса на абсолютное сходство s1 (нормированное в
    диапазон 0.80..0.96 — SAD по бинарным глифам сжат в узкую полосу) и
    0.25 на отрыв от второго символа. Усредняется по сегментам.
    Возвращает dict: text, conf, method, available, details.
    """
    result = {
        "text": "",
        "conf": 0.0,
        "method": "template",
        "available": bool(templates),
        "details": [],
    }
    if not templates:
        return result

    if size is None:
        size = next(iter(next(iter(templates.values())))).size
    if charset:
        templates = {c: t for c, t in templates.items() if c in charset}
        if not templates:
            return result

    if boxes is None:
        boxes = segment(img)
    if not boxes:
        return result

    # предсчёт бинарных версий один раз на вызов
    tpl_bin = {}
    for ch, lst in templates.items():
        tpl_bin[ch] = [_to_bin1(t) for t in lst]

    # грубый этап: по одному эталону на символ, чтобы не гонять все 250+
    reps = {ch: lst[0] for ch, lst in templates.items()}
    reps_bin = {ch: tpl_bin[ch][0] for ch in templates}
    # сравнение со всеми эталонами стоит ~40 мс на картинку — шортлист
    # экономит копейки, но теряет 6 п.п. точности, поэтому не нужен
    shortlist_size = len(templates)

    chars = []
    confs = []
    details = []

    for b in boxes:
        seg = img.crop((b[0], b[1], b[2], b[3]))
        glyph = normalize_glyph(seg, size=size)
        g1 = _to_bin1(glyph) if metric == "iou" else None

        # 1) грубо: репрезентативный эталон каждого символа
        coarse = []
        for ch, rep in reps.items():
            s = _similarity(glyph, rep, metric,
                            a1=g1, b1=reps_bin[ch] if metric == "iou" else None)
            coarse.append((s, ch))
        coarse.sort(reverse=True)
        shortlist = [ch for _, ch in coarse[:shortlist_size]]

        # 2) точно: только по попавшим в шортлист, всеми их эталонами
        scores = {}
        for ch in shortlist:
            best = -1.0
            bins = tpl_bin[ch]
            for i, t in enumerate(templates[ch]):
                s = _similarity(glyph, t, metric,
                                a1=g1, b1=bins[i] if metric == "iou" else None)
                if s > best:
                    best = s
            scores[ch] = best

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        ch1, s1 = ranked[0]
        s2 = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = (s1 - s2) / max(s1, 1e-6)
        # SAD-сходство сжато в 0.85..1.0, поэтому абсолютный уровень важнее
        # отрыва: 0.75 веса на s1 (нормированный в 0.80..0.96) и 0.25 на margin.
        s1c = max(0.0, min(1.0, (s1 - 0.80) / 0.16))
        mc = max(0.0, min(1.0, margin * 4.0))
        conf = 0.75 * s1c + 0.25 * mc

        chars.append(ch1)
        confs.append(conf)
        details.append({
            "box": b,
            "char": ch1,
            "score": round(s1, 4),
            "runner_up": ranked[1][0] if len(ranked) > 1 else "",
            "runner_up_score": round(s2, 4),
            "conf": round(conf, 4),
        })

    result["text"] = "".join(chars)
    result["conf"] = (sum(confs) / len(confs)) if confs else 0.0
    result["details"] = details
    return result


# =========================================================================
# банк шаблонов
# =========================================================================

def build_templates(fonts=None, charset=DEFAULT_CHARSET, render_size=48,
                    size=GLYPH_SIZE, rotations=(0,), verbose=False):
    """Строит {символ: [глиф, ...]} из системных TTF."""
    fonts = fonts or DEFAULT_TEMPLATE_FONTS
    templates = {}
    used = []
    for fp in fonts:
        if not os.path.exists(fp):
            continue
        try:
            font = ImageFont.truetype(fp, render_size)
        except Exception:
            continue
        used.append(fp)
        for ch in charset:
            for ang in rotations:
                cell = Image.new("L", (render_size * 2, render_size * 2), 255)
                ImageDraw.Draw(cell).text(
                    (render_size // 2, render_size // 2), ch, font=font, fill=0
                )
                if ang:
                    cell = cell.rotate(ang, resample=Image.BICUBIC, fillcolor=255)
                g = normalize_glyph(cell, size=size)
                if g.getbbox() is None:
                    continue
                templates.setdefault(ch, []).append(g)
        if verbose:
            print("  +", os.path.basename(fp), flush=True)
    return templates


def save_templates(templates, path=DEFAULT_TEMPLATE_PATH):
    blob = {}
    for ch, lst in templates.items():
        buf = []
        for t in lst:
            bio = io.BytesIO()
            t.save(bio, "PNG")
            buf.append(bio.getvalue())
        blob[ch] = buf
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump({"version": __version__, "size": list(GLYPH_SIZE),
                     "chars": blob}, f, protocol=4)
    os.replace(tmp, path)
    return path


def load_templates(path=DEFAULT_TEMPLATE_PATH):
    with open(path, "rb") as f:
        blob = pickle.load(f)
    chars = blob.get("chars", blob)
    out = {}
    for ch, bufs in chars.items():
        imgs = []
        for b in bufs:
            im = Image.open(io.BytesIO(b))
            im.load()
            imgs.append(im.convert("L"))
        out[ch] = imgs
    return out


def ensure_templates(path=DEFAULT_TEMPLATE_PATH, rebuild=False, verbose=False):
    """Возвращает банк шаблонов, строя и кешируя его при необходимости."""
    if not rebuild and os.path.exists(path):
        try:
            return load_templates(path)
        except Exception:
            pass
    if verbose:
        print("building template bank...", flush=True)
    tpl = build_templates(verbose=verbose)
    try:
        save_templates(tpl, path)
    except OSError:
        pass
    return tpl


def learn(text, img_bytes, path=DEFAULT_TEMPLATE_PATH):
    """Дообучение банка реальной капчей: добавляет нарезанные глифы.

    Нужен один-два реальных сэмпла, чтобы шаблоны совпали с настоящим шрифтом.
    """
    tpl = load_templates(path) if os.path.exists(path) else {}
    pre = preprocess(_load(img_bytes))
    boxes = segment(pre)
    if len(boxes) != len(text):
        raise ValueError(
            "сегментов %d, а метки %d — подбери другой порог/маску"
            % (len(boxes), len(text))
        )
    for b, ch in zip(boxes, text):
        tpl.setdefault(ch, []).append(normalize_glyph(pre.crop(tuple(b))))
    save_templates(tpl, path)
    return len(text)


# =========================================================================
# 5. solve
# =========================================================================

def _plausibility(text, n_seg, charset):
    if not text:
        return 0.0
    if any(c not in charset for c in text):
        return 0.0
    score = 1.0
    if n_seg and len(text) != n_seg:
        score *= 0.55
        score -= 0.08 * abs(len(text) - n_seg)
    return max(0.0, score)


def solve(img_bytes, templates=None, charset=DEFAULT_CHARSET, prefer=None,
          use_tesseract=True, use_template=True, metric="sad", debug=False,
          fast=True, min_template_conf=0.45, morph_variants=(3, 5)):
    """Главная функция: пробует методы и варианты предобработки, отдаёт лучший.

    Порядок:
      1. для каждого размера морфологического ядра (3 и 5) — preprocess,
         segment, ocr_template; берётся вариант с лучшим score;
      2. tesseract запускается только если шаблон не уверен (каскад fast=True);
      3. победитель — максимальный score = conf * правдоподобие длины.

    Ядро 3 сохраняет тонкие штрихи на чистой картинке, ядро 5 снимает
    линии помех. Поэтому считаются оба, а не выбирается одно на глаз.

    Возвращает dict:
        text, confidence, score, method, segments, candidates, boxes,
        elapsed_ms, charset
    """
    t_start = time.time()
    img = _load(img_bytes)

    if templates is None and use_template:
        try:
            templates = ensure_templates()
        except Exception:
            templates = None

    candidates = []
    per_variant = []
    fallback = None

    for m in (morph_variants or (3,)):
        pre = preprocess(img, morph=m)
        boxes = segment(pre)
        n_seg = len(boxes)

        # правдоподобная нарезка: 3-8 символов, как в реальной капче
        plausible = 3 <= n_seg <= 8

        res = None
        if use_template and templates:
            res = ocr_template(pre, templates, metric=metric, charset=charset,
                               boxes=boxes)
            if res["text"]:
                res["score"] = (res["conf"]
                                * _plausibility(res["text"], n_seg, charset)
                                * (1.10 if n_seg and len(res["text"]) == n_seg else 1.0)
                                * (1.0 if plausible else 0.4))
                res["morph"] = m
                res["boxes"] = boxes
                candidates.append(res)

        entry = {"morph": m, "n_seg": n_seg, "plausible": plausible,
                 "pre": pre, "boxes": boxes, "res": res,
                 "conf": res["conf"] if res else 0.0,
                 "score": res["score"] if res else 0.0}
        per_variant.append(entry)
        if fallback is None or (plausible, entry["score"]) > (
                fallback["plausible"], fallback["score"]):
            fallback = entry

    # Приоритет у ядра 3: оно сохраняет тонкие штрихи, а на чистой картинке
    # это главное. Ядро 5 (снимает линии) берём только если оно выигрывает
    # с запасом более 25% — иначе случайные победы на неоднозначных символах
    # (o/0, l/1/i) только портят результат.
    base3 = next((e for e in per_variant if e["morph"] == 3), None)
    alt5 = next((e for e in per_variant if e["morph"] == 5), None)

    if base3 is not None and base3["plausible"]:
        chosen = base3
        if alt5 is not None and alt5["plausible"] and alt5["score"] > base3["score"] * 1.25:
            chosen = alt5
    elif alt5 is not None and alt5["plausible"]:
        chosen = alt5
    else:
        chosen = fallback

    best_pre, best_boxes = chosen["pre"], chosen["boxes"]
    # оставляем кандидатов только от выбранного варианта предобработки
    same = [c for c in candidates if c.get("morph") == chosen["morph"]]
    if same:
        candidates = same

    n_seg = len(best_boxes)

    if use_tesseract and HAS_TESSERACT:
        skip = False
        if fast and candidates:
            top = max(candidates, key=lambda c: c.get("score", 0.0))
            if (top["method"] == "template" and n_seg
                    and len(top["text"]) == n_seg
                    and top["conf"] >= min_template_conf):
                skip = True
        if not skip and best_pre is not None:
            res = ocr_tesseract(best_pre, charset=charset, expected_len=n_seg)
            if res["text"]:
                res["score"] = (res["conf"]
                                * _plausibility(res["text"], n_seg, charset)
                                * (1.0 if 3 <= n_seg <= 8 else 0.5))
                res["morph"] = None
                res["boxes"] = best_boxes
                candidates.append(res)
            elif debug:
                candidates.append(dict(res, score=0.0, morph=None, boxes=best_boxes))

    if prefer:
        for c in candidates:
            if c.get("method") == prefer:
                c["score"] = c.get("score", 0.0) * 1.5

    candidates.sort(key=lambda c: -c.get("score", 0.0))
    best = candidates[0] if candidates else None

    return {
        "text": best["text"] if best else "",
        "confidence": round(float(best.get("conf", 0.0)), 4) if best else 0.0,
        "score": round(float(best.get("score", 0.0)), 4) if best else 0.0,
        "method": best.get("method", "none") if best else "none",
        "segments": n_seg,
        "charset": charset,
        "boxes": best_boxes,
        "morph": best.get("morph") if best else None,
        "candidates": [
            {
                "method": c.get("method"),
                "text": c.get("text", ""),
                "conf": round(float(c.get("conf", 0.0)), 4),
                "score": round(float(c.get("score", 0.0)), 4),
                "morph": c.get("morph"),
            }
            for c in candidates
        ],
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }


def answer_json(text):
    """Готовый payload для screenId 26: {"t":0,"s":"<текст>"}."""
    return json.dumps({"t": 0, "s": text}, separators=(",", ":"))


def solve_file(path, **kw):
    with open(path, "rb") as f:
        return solve(f.read(), **kw)


# =========================================================================
# синтетика и бенчмарк
# =========================================================================

def synth_captcha(rnd=None, text=None, size=(190, 64), fonts=None,
                  charset=DEFAULT_CHARSET, length=None, noise=0.05,
                  lines=1, dots=30, rotate=10, blur=0.6, jpeg=True,
                  quality=80, gap=(4, 9), font_size=(30, 38)):
    """Рисует синтетическую капчу, похожую на реальную.

    Символы не накладываются друг на друга (как в настоящей капче),
    у каждого свой поворот и вертикальный сдвиг; сверху 1-2 тонкие линии,
    точки, гауссов шум и JPEG-сжатие.

    Возвращает (text, bytes).
    """
    rnd = rnd or random.Random()
    fonts = fonts or DEFAULT_TEMPLATE_FONTS
    length = length or rnd.randint(4, 6)
    text = text or "".join(rnd.choice(charset) for _ in range(length))

    w, h = size

    # 1. каждый символ рендерится на своей подложке
    glyphs = []
    for ch in text:
        try:
            font = ImageFont.truetype(rnd.choice(fonts), rnd.randint(*font_size))
        except Exception:
            font = ImageFont.load_default()
        cell = Image.new("L", (90, 90), 255)
        ImageDraw.Draw(cell).text((14, 10), ch, font=font, fill=0)
        bbox = ImageOps.invert(cell).getbbox()
        if not bbox:
            continue
        cell = cell.crop(bbox)
        if rnd.random() < 0.75:
            cell = cell.rotate(rnd.uniform(-rotate, rotate),
                               resample=Image.BICUBIC, fillcolor=255, expand=True)
        glyphs.append(cell)

    if not glyphs:
        raise ValueError("не удалось отрисовать ни одного символа")

    gaps = [rnd.randint(*gap) for _ in glyphs]

    def total_width():
        return sum(g.width for g in glyphs) + sum(gaps[:len(glyphs) - 1])

    # 2. если не влезает — ужимаем пропорционально
    tw = total_width()
    if tw > w - 10:
        k = (w - 10) / float(tw)
        glyphs = [g.resize((max(4, int(g.width * k)), max(4, int(g.height * k))),
                           Image.LANCZOS) for g in glyphs]
        gaps = [max(2, int(gp * k)) for gp in gaps]
        tw = total_width()

    # 3. фон: градиент + пятна
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    base = rnd.randint(200, 238)
    amp = rnd.randint(4, 16)
    for y in range(h):
        v = int(base - amp * math.sin(math.pi * y / max(1, h - 1)))
        d.line([(0, y), (w, y)], fill=max(0, min(255, v)))
    for _ in range(rnd.randint(2, 6)):
        cx, cy = rnd.randint(0, w), rnd.randint(0, h)
        r = rnd.randint(5, 16)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rnd.randint(205, 250))

    # 4. расстановка по общей базовой линии (как в реальной капче)
    max_h = max(g.height for g in glyphs)
    baseline = max(0, (h - max_h) // 2)
    x = max(2, (w - tw) // 2)
    ink = rnd.randint(0, 60)
    for i, g in enumerate(glyphs):
        mask = g.point(lambda p: 255 if p < 190 else 0, mode="L")
        y = baseline + (max_h - g.height) + rnd.randint(-2, 2)
        y = max(0, min(h - g.height, y))
        img.paste(ink, (x, y), mask)
        x += g.width + gaps[i]

    # 5. линии помех (тонкие, умеренный наклон)
    for _ in range(lines):
        if rnd.random() < 0.5:
            x0, x1 = 0, w
        else:
            x0, x1 = rnd.randint(0, w // 3), rnd.randint(2 * w // 3, w)
        y0, y1 = rnd.randint(0, h), rnd.randint(0, h)
        d.line([(x0, y0), (x1, y1)], fill=rnd.randint(20, 110), width=1)

    # 6. точки
    for _ in range(dots):
        d.point((rnd.randint(0, w - 1), rnd.randint(0, h - 1)),
                fill=rnd.randint(0, 120))

    # 7. шум и размытие
    if noise > 0:
        nimg = Image.effect_noise(size, rnd.randint(20, 40))
        img = Image.blend(img, nimg, noise)
    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    bio = io.BytesIO()
    if jpeg:
        img.convert("RGB").save(bio, "JPEG", quality=quality)
    else:
        img.save(bio, "PNG")
    return text, bio.getvalue()


def _char_acc(pred, truth):
    hit = sum(1 for a, b in zip(pred, truth) if a == b)
    return hit, len(truth)


def bench(n=20, seed=20240501, held_out=False, out_dir=None, quiet=False,
          metric="sad"):
    """Прогоняет n синтетических капч, печатает точность и время."""
    rnd = random.Random(seed)
    fonts = HELDOUT_FONTS if held_out else None
    tpl = ensure_templates()
    rows = []
    exact = 0
    ch_hit = ch_all = 0
    times = []
    methods = {}

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    for i in range(n):
        truth, data = synth_captcha(rnd=rnd, fonts=fonts)
        t0 = time.time()
        res = solve(data, templates=tpl, metric=metric)
        dt = time.time() - t0
        times.append(dt)

        ok = res["text"] == truth
        exact += int(ok)
        h, t = _char_acc(res["text"], truth)
        ch_hit += h
        ch_all += t
        methods[res["method"]] = methods.get(res["method"], 0) + 1
        rows.append((i + 1, truth, res["text"], ok, res["confidence"],
                     res["method"], dt))

        if out_dir:
            with open(os.path.join(out_dir, "%02d_%s_%s.jpg"
                                   % (i + 1, truth, res["text"] or "empty")), "wb") as f:
                f.write(data)

    if not quiet:
        print("%-3s %-8s %-8s %-4s %-6s %-10s %s" %
              ("#", "truth", "pred", "ok", "conf", "method", "ms"))
        for r in rows:
            print("%-3d %-8s %-8s %-4s %-6.2f %-10s %d" %
                  (r[0], r[1], r[2] or "-", "+" if r[3] else "-", r[4], r[5],
                   int(r[6] * 1000)))

    summary = {
        "n": n,
        "exact": exact,
        "exact_acc": exact / float(n),
        "char_acc": (ch_hit / float(ch_all)) if ch_all else 0.0,
        "avg_ms": int(sum(times) / len(times) * 1000),
        "methods": methods,
        "held_out": held_out,
    }
    if not quiet:
        print("\n--- итог ---")
        print("точных совпадений: %d/%d = %.1f%%" %
              (exact, n, 100.0 * exact / n))
        print("точность по символам: %.1f%% (%d/%d)" %
              (100.0 * ch_hit / ch_all, ch_hit, ch_all) if ch_all else "n/a")
        print("среднее время: %d мс" % summary["avg_ms"])
        print("методы:", methods)
    return summary


# =========================================================================
# CLI
# =========================================================================

def _main(argv):
    import argparse

    ap = argparse.ArgumentParser(description="Black Russia captcha solver")
    sub = ap.add_subparsers(dest="cmd")

    p_solve = sub.add_parser("solve", help="распознать файл")
    p_solve.add_argument("path")
    p_solve.add_argument("--json", action="store_true")
    p_solve.add_argument("--charset", default=DEFAULT_CHARSET)
    p_solve.add_argument("--metric", default="sad", choices=("sad", "iou", "cos"))

    p_bench = sub.add_parser("bench", help="точность на синтетике")
    p_bench.add_argument("-n", type=int, default=20)
    p_bench.add_argument("--seed", type=int, default=20240501)
    p_bench.add_argument("--held-out", action="store_true")
    p_bench.add_argument("--out")
    p_bench.add_argument("--metric", default="sad", choices=("sad", "iou", "cos"))

    p_gen = sub.add_parser("gen", help="сгенерировать синтетические капчи")
    p_gen.add_argument("-n", type=int, default=20)
    p_gen.add_argument("--out", default="captcha_samples")
    p_gen.add_argument("--seed", type=int, default=20240501)

    p_tpl = sub.add_parser("templates", help="банк шаблонов")
    p_tpl.add_argument("--rebuild", action="store_true")
    p_tpl.add_argument("--path", default=DEFAULT_TEMPLATE_PATH)

    p_learn = sub.add_parser("learn", help="дообучить банк реальной капчей")
    p_learn.add_argument("path")
    p_learn.add_argument("text")
    p_learn.add_argument("--path-tpl", default=DEFAULT_TEMPLATE_PATH)

    p_env = sub.add_parser("env", help="что доступно в системе")

    args = ap.parse_args(argv)

    if args.cmd == "solve":
        res = solve_file(args.path, charset=args.charset, metric=args.metric,
                         debug=True)
        if args.json:
            print(json.dumps({k: v for k, v in res.items() if k != "boxes"},
                             ensure_ascii=False, indent=2))
        else:
            print("text      :", res["text"] or "-")
            print("confidence:", res["confidence"])
            print("method    :", res["method"])
            print("segments  :", res["segments"])
            print("elapsed   :", res["elapsed_ms"], "ms")
            for c in res["candidates"]:
                print("  cand %-10s %-8r conf=%.3f score=%.3f" %
                      (c["method"], c["text"], c["conf"], c["score"]))
        return 0

    if args.cmd == "bench":
        bench(args.n, seed=args.seed, held_out=args.held_out, out_dir=args.out,
              metric=args.metric)
        return 0

    if args.cmd == "gen":
        rnd = random.Random(args.seed)
        os.makedirs(args.out, exist_ok=True)
        for i in range(args.n):
            text, data = synth_captcha(rnd=rnd)
            p = os.path.join(args.out, "%02d_%s.jpg" % (i + 1, text))
            with open(p, "wb") as f:
                f.write(data)
        print("saved %d -> %s" % (args.n, args.out))
        return 0

    if args.cmd == "templates":
        tpl = ensure_templates(args.path, rebuild=args.rebuild, verbose=True)
        n = sum(len(v) for v in tpl.values())
        print("chars=%d templates=%d path=%s" % (len(tpl), n, args.path))
        return 0

    if args.cmd == "learn":
        with open(args.path, "rb") as f:
            data = f.read()
        k = learn(args.text, data, path=args.path_tpl)
        print("learned %d glyphs" % k)
        return 0

    if args.cmd == "env":
        print("python     :", sys.version.split()[0])
        print("Pillow     :", getattr(Image, "__version__", "?"))
        print("numpy      :", HAS_NUMPY)
        print("cv2        :", HAS_CV2)
        print("pytesseract:", HAS_PYTESSERACT)
        print("tesseract  :", TESSERACT_BIN or "нет")
        if TESSERACT_BIN:
            try:
                out = subprocess.run([TESSERACT_BIN, "--version"],
                                     capture_output=True, text=True).stdout
                print("             ", out.splitlines()[0] if out else "?")
            except Exception:
                pass
        print("templates  :", DEFAULT_TEMPLATE_PATH,
              "exists" if os.path.exists(DEFAULT_TEMPLATE_PATH) else "нет")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
