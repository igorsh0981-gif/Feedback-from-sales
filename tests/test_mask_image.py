import io
import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.masking.image import mask_image

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LANGS = os.getenv("TEST_TESSERACT_LANGS", "rus+eng")

pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None or not os.path.exists(FONT), reason="нужны tesseract и шрифт DejaVu"
)


def _screen(lines: list[str]) -> bytes:
    font = ImageFont.truetype(FONT, 30)
    img = Image.new("RGB", (900, 90 + 60 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        d.text((40, 40 + 60 * i), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ocr(data: bytes) -> str:
    import pytesseract

    return pytesseract.image_to_string(Image.open(io.BytesIO(data)), lang=LANGS)


def test_receipt_card_phone_name_are_painted():
    raw = _screen([
        "Перевод выполнен",
        "Сумма 1 250 000 сум",
        "Карта 8600 1234 5678 9012",
        "Получатель: Иванов Пётр",
        "Телефон +998 90 123 45 67",
        "Номер операции 123456789012",
    ])
    res = mask_image(raw, LANGS)
    assert res.verified, res.reason
    assert res.counts["card"] == 1
    assert res.counts["phone"] == 1
    assert res.counts["fio"] == 1
    assert res.counts["digits"] >= 1
    after = _ocr(res.image_bytes)
    assert "5678" not in after
    assert "Иванов" not in after
    assert "123 45" not in after and "45 67" not in after
    assert "123456789012" not in after
    # Сумма и заголовок остаются читаемыми
    assert "Перевод" in after
    assert "250" in after
    assert "[•••]" in res.ocr_text_masked
    assert "5678" not in res.ocr_text_masked


def test_label_on_previous_line():
    raw = _screen(["Перевод выполнен успешно", "Получатель:", "Сидорова Анна", "Сумма 50 000 сум"])
    res = mask_image(raw, LANGS)
    after = _ocr(res.image_bytes)
    assert "Сидорова" not in after
    assert "Сумма" in after


def test_blank_image_is_not_verified():
    img = Image.new("RGB", (400, 700), (240, 240, 240))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    res = mask_image(buf.getvalue(), LANGS)
    assert not res.verified
    assert res.reason


def test_broken_file():
    res = mask_image(b"not an image", LANGS)
    assert not res.verified and res.image_bytes == b""
