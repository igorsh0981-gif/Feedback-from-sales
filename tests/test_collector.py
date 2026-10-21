import io
import shutil
from datetime import datetime, timezone

import pytest
from aiogram.types import Message
from PIL import Image, ImageDraw, ImageFont

from app.bot.collector import Collector
from app.util import jloads

from .helpers import make_repo

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def msg(mid, text=None, photo=None, media_group=None, chat=-1001, user=7, reply=None):
    data = {
        "message_id": mid, "date": datetime.now(timezone.utc),
        "chat": {"id": chat, "type": "supergroup", "title": "Android · Ташкент"},
        "from": {"id": user, "is_bot": False, "first_name": "Азиз", "username": "aziz"},
    }
    if text and not photo:
        data["text"] = text
    if photo:
        data["photo"] = [{"file_id": photo, "file_unique_id": photo + "u", "width": 800, "height": 600}]
        if text:
            data["caption"] = text
    if media_group:
        data["media_group_id"] = media_group
    if reply:
        data["reply_to_message"] = {"message_id": reply, "date": datetime.now(timezone.utc),
                                    "chat": data["chat"], "text": "x"}
    return Message.model_validate(data)


def screen_bytes():
    font = ImageFont.truetype(FONT, 30)
    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    for i, t in enumerate(["Перевод выполнен", "Карта 8600 1234 5678 9012", "Сумма 50 000 сум"]):
        d.text((40, 40 + 70 * i), t, fill="black", font=font)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return b.getvalue()


async def test_text_masked_and_inactive_chat_ignored():
    repo = await make_repo()
    col = Collector(repo, "rus+eng", None, None)
    rec = col.store_message(msg(1, "Перевёл на 8600 1234 5678 9012, не дошло", reply=None))
    assert "5678" not in rec["text_masked"] and rec["mask_summary"] == "карта ×1"
    assert rec["link"] == "https://t.me/c/1/1"
    assert col.store_message(msg(1, "дубль")) is None
    assert col.store_message(msg(2, "чужой чат", chat=-1999)) is None
    assert repo.get("Participants", "7")["username"] == "aziz"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="нужен tesseract")
async def test_album_merged_and_screens_masked():
    repo = await make_repo()
    uploaded = []

    async def down(fid):
        return screen_bytes()

    async def up(data, caption):
        uploaded.append(data)
        return f"masked-{len(uploaded)}"

    col = Collector(repo, "rus+eng", down, up)
    col.store_message(msg(10, "Не проходит перевод", photo="f1", media_group="g1"))
    col.store_message(msg(11, None, photo="f2", media_group="g1"))
    await col.drain()
    rows = repo.rows("Messages")
    assert len(rows) == 1
    rec = rows[0]
    screens = jloads(rec["screens"], [])
    assert [s["status"] for s in screens] == ["ok", "ok"]
    assert rec["screen_status"] == "ok" and rec["raw_files"] == ""
    assert "5678" not in screens[0]["ocr"]
    assert "карта ×2" in rec["mask_summary"]
    assert len(uploaded) == 2


async def test_edit_requeues_unaccepted_issue():
    repo = await make_repo()
    col = Collector(repo, "rus+eng", None, None)
    rec = col.store_message(msg(20, "Перевод завис"))
    repo.insert("Issues", {"issue_id": "FP-001", "status": "Новое"})
    repo.update("Messages", rec["msg_id"], {"state": "Учтено", "issue_id": "FP-001"})
    edited = msg(20, "Вклад не открывается")
    edited = edited.model_copy(update={"edit_date": datetime.now(timezone.utc)})
    col.store_edit(edited)
    after = repo.get("Messages", rec["msg_id"])
    assert after["state"] == "В очереди" and after["issue_id"] == ""
    assert "было в FP-001" in after["state_reason"]
