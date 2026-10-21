"""Обезличивание скриншотов (раздел 6 ТЗ).

1. Tesseract распознаёт текст и координаты слов.
2. К строкам применяются те же правила, что и к тексту сообщений.
3. Найденные области закрашиваются; дополнительно — любое слово с 8+ цифрами.
4. Если скрин прочитан неуверенно, он помечается «не проверен» и не уходит в Claude.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageOps

from .text import CARD_RE, is_fio_label_line, mask_text, summarize

log = logging.getLogger(__name__)

MAX_SIDE = 2000
MIN_CONFIDENT_WORDS = 3
MIN_MEAN_CONF = 55.0
LONG_DIGITS_RE = re.compile(r"\d")
CAPS_NAME_LINE_RE = re.compile(r"^(?:[A-Z'ʻ’]{2,}\s+){1,2}[A-Z'ʻ’]{2,}$")


@dataclass
class Word:
    text: str
    conf: float
    box: tuple[int, int, int, int]  # left, top, right, bottom


@dataclass
class ImageMaskResult:
    image_bytes: bytes
    verified: bool
    counts: Counter = field(default_factory=Counter)
    ocr_text_masked: str = ""
    reason: str = ""
    width: int = 0
    height: int = 0

    @property
    def summary(self) -> str:
        return summarize(self.counts)


def _ocr_lines(img: Image.Image, langs: str) -> list[list[Word]]:
    import pytesseract

    data = pytesseract.image_to_data(img, lang=langs, output_type=pytesseract.Output.DICT, config="--psm 3")
    lines: dict[tuple[int, int, int], list[Word]] = {}
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        left, top = int(data["left"][i]), int(data["top"][i])
        box = (left, top, left + int(data["width"][i]), top + int(data["height"][i]))
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        lines.setdefault(key, []).append(Word(text, conf, box))
    ordered = sorted(lines.values(), key=lambda ws: (min(w.box[1] for w in ws), min(w.box[0] for w in ws)))
    return [sorted(ws, key=lambda w: w.box[0]) for ws in ordered]


def _line_text(words: list[Word]) -> tuple[str, list[tuple[int, int]]]:
    parts, offsets, pos = [], [], 0
    for w in words:
        if parts:
            pos += 1
        offsets.append((pos, pos + len(w.text)))
        parts.append(w.text)
        pos += len(w.text)
    return " ".join(parts), offsets


def _union(boxes: list[tuple[int, int, int, int]], pad: int = 4) -> tuple[int, int, int, int]:
    return (
        min(b[0] for b in boxes) - pad,
        min(b[1] for b in boxes) - pad,
        max(b[2] for b in boxes) + pad,
        max(b[3] for b in boxes) + pad,
    )


def _prepare(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        if img.mode in ("RGBA", "LA"):
            background.paste(img, mask=img.split()[-1])
        else:
            background.paste(img.convert("RGB"))
        img = background
    elif img.mode == "L":
        img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


def mask_image(raw: bytes, langs: str = "rus+uzb+uzb_cyrl+eng") -> ImageMaskResult:
    """Возвращает закрашенное изображение (JPEG) и признак, что скрин прочитан уверенно."""
    try:
        img = _prepare(raw)
    except Exception as exc:  # повреждённый файл
        log.warning("cannot open image: %s", exc)
        return ImageMaskResult(b"", False, reason="Файл изображения не читается")

    lines = _ocr_lines(img, langs)
    words = [w for line in lines for w in line]
    confident = [w for w in words if w.conf >= 60]
    scored = [w.conf for w in words if w.conf >= 0]
    mean_conf = sum(scored) / len(scored) if scored else 0.0

    counts: Counter = Counter()
    boxes: list[tuple[int, int, int, int]] = []
    masked_lines: list[str] = []
    full_text = "\n".join(_line_text(line)[0] for line in lines)
    has_card = bool(CARD_RE.search(full_text))

    mask_next_as_name = False
    for line in lines:
        text, offsets = _line_text(line)
        res = mask_text(text, force_expiry=has_card)
        counts.update(res.counts)
        hit: set[int] = set()
        for start, end, _kind in res.spans:
            for idx, (ws, we) in enumerate(offsets):
                if ws < end and we > start:
                    hit.add(idx)
        # Подпись «Получатель:» на отдельной строке — имя в следующей строке.
        if mask_next_as_name and not any(ch.isdigit() for ch in text) and 1 <= len(line) <= 4:
            hit.update(range(len(line)))
            counts["fio"] += 1
        mask_next_as_name = is_fio_label_line(text)
        # Имя держателя на изображении карты: строка из 2–3 слов латиницей в верхнем регистре.
        if has_card and CAPS_NAME_LINE_RE.match(text):
            if not hit.issuperset(range(len(line))):
                counts["fio"] += 1
            hit.update(range(len(line)))
        # Любое слово с 8+ цифрами.
        for idx, w in enumerate(line):
            if idx not in hit and len(LONG_DIGITS_RE.findall(w.text)) >= 8:
                hit.add(idx)
                counts["digits"] += 1
        for idx in sorted(hit):
            boxes.append(_union([line[idx].box]))
        # Текст для анализа: закрашенные слова заменяем меткой, как на изображении.
        masked_line = " ".join("[•••]" if idx in hit else w.text for idx, w in enumerate(line))
        masked_lines.append(masked_line)

    draw = ImageDraw.Draw(img)
    for left, top, right, bottom in boxes:
        draw.rectangle([max(0, left), max(0, top), min(img.width, right), min(img.height, bottom)], fill=(32, 41, 36))

    reason = ""
    verified = True
    if len(confident) < MIN_CONFIDENT_WORDS:
        verified = False
        reason = "На скрине почти нет распознаваемого текста"
    elif mean_conf < MIN_MEAN_CONF:
        verified = False
        reason = f"Низкое качество распознавания ({mean_conf:.0f}%)"

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return ImageMaskResult(
        image_bytes=out.getvalue(),
        verified=verified,
        counts=counts,
        ocr_text_masked="\n".join(l for l in masked_lines if l.strip())[:1500],
        reason=reason,
        width=img.width,
        height=img.height,
    )
