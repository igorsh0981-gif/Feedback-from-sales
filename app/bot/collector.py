"""Приём сообщений из чатов фокус-группы (раздел 5 ТЗ) и обезличивание скриншотов."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.types import Message

from ..constants import LINKED_STATES, MSG_QUEUED
from ..masking.image import mask_image
from ..masking.text import mask_text, merge_summaries
from ..services import recount_issue
from ..storage.repo import Repo
from ..util import chat_link, iso, jdumps, jloads

log = logging.getLogger(__name__)

Downloader = Callable[[str], Awaitable[bytes]]
Uploader = Callable[[bytes, str], Awaitable[str]]


def message_kind(message: Message) -> str:
    if message.photo:
        return "photo"
    if message.document:
        mime = message.document.mime_type or ""
        return "photo" if mime.startswith("image/") else "document"
    for kind in ("voice", "video", "video_note", "audio", "sticker", "animation"):
        if getattr(message, kind, None):
            return kind
    if message.text or message.caption:
        return "text"
    return "other"


def image_file_id(message: Message) -> str:
    if message.photo:
        return message.photo[-1].file_id
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id
    return ""


class Collector:
    def __init__(self, repo: Repo, langs: str, downloader: Downloader | None, uploader: Uploader | None,
                 ocr_concurrency: int = 2):
        self.repo = repo
        self.langs = langs
        self.downloader = downloader
        self.uploader = uploader
        self._sem = asyncio.Semaphore(ocr_concurrency)
        self._albums: dict[tuple[str, str], str] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- чаты
    def chat_active(self, chat_id: int | str) -> bool:
        rec = self.repo.get("Chats", str(chat_id))
        return bool(rec and rec.get("active") == "1")

    # ---------------------------------------------------------------- сообщения
    def store_message(self, message: Message) -> dict | None:
        """Сохраняет сообщение сразу при получении. Без await внутри — атомарно для альбомов."""
        chat_id = str(message.chat.id)
        if not self.chat_active(chat_id):
            return None
        msg_id = f"{chat_id}:{message.message_id}"
        if self.repo.get("Messages", msg_id):
            return None  # повторная доставка
        kind = message_kind(message)
        text = message.text or message.caption or ""
        if kind == "other" and not text:
            return None  # служебное событие: добавили бота или участника, закрепили сообщение, сменили название и т. п.
        masked = mask_text(text)
        file_id = image_file_id(message)
        album_key = (chat_id, message.media_group_id or "")

        if message.media_group_id and album_key in self._albums:
            rec = self.repo.get("Messages", self._albums[album_key])
            if rec:
                raw = jloads(rec.get("raw_files", ""), [])
                if file_id:
                    raw.append(file_id)
                new_text = rec.get("text_masked", "")
                if masked.text and masked.text not in new_text:
                    new_text = (new_text + "\n" + masked.text).strip()
                self.repo.update("Messages", rec["msg_id"], {
                    "raw_files": jdumps(raw), "screen_status": "pending" if raw else rec.get("screen_status", ""),
                    "text_masked": new_text, "mask_summary": merge_summaries(rec.get("mask_summary", ""), masked.summary),
                    "updated_at": iso(),
                })
                if file_id:
                    self._spawn(rec["msg_id"], file_id)
                return rec

        user = message.from_user
        if user:
            author_id, author_name = str(user.id), user.full_name or ""
        elif message.author_signature:  # пост в канале с подписью автора
            author_id = f"{chat_id}:{message.author_signature}"
            author_name = message.author_signature
        elif message.sender_chat:
            author_id, author_name = str(message.sender_chat.id), message.sender_chat.title or ""
        else:
            author_id, author_name = "", ""
        participant = self.repo.get("Participants", author_id) if author_id else None
        chat = self.repo.get("Chats", chat_id) or {}
        if user and not participant:
            participant = self.repo.insert("Participants", {
                "telegram_id": author_id, "name": author_name, "username": user.username or "",
                "platform": "", "device": "", "app_version": "", "updated_at": iso(),
            })
        thread_id = str(message.message_thread_id) if message.is_topic_message and message.message_thread_id else ""
        reply_to = ""
        if message.reply_to_message and not (
            message.is_topic_message and message.reply_to_message.message_id == message.message_thread_id
        ):
            reply_to = str(message.reply_to_message.message_id)
        rec = self.repo.insert("Messages", {
            "msg_id": msg_id,
            "chat_id": chat_id,
            "chat_title": chat.get("title") or message.chat.title or "",
            "message_id": str(message.message_id),
            "thread_id": thread_id,
            "reply_to": reply_to,
            "media_group_id": message.media_group_id or "",
            "author_id": author_id,
            "author_name": author_name,
            "sent_at": iso(message.date),
            "kind": kind,
            "platform": (participant or {}).get("platform", "") or "",
            "device": (participant or {}).get("device", "") or "",
            "app_version": (participant or {}).get("app_version", "") or "",
            "text_masked": masked.text,
            "screens": "",
            "screen_status": "pending" if file_id else "",
            "raw_files": jdumps([file_id]) if file_id else "",
            "mask_summary": masked.summary,
            "link": chat_link(chat_id, message.message_id, thread_id),
            "state": MSG_QUEUED,
            "updated_at": iso(),
        })
        if message.media_group_id:
            self._albums[album_key] = msg_id
            if len(self._albums) > 500:
                self._albums.pop(next(iter(self._albums)))
        if file_id:
            self._spawn(msg_id, file_id)
        return rec

    def store_edit(self, message: Message) -> dict | None:
        msg_id = f"{message.chat.id}:{message.message_id}"
        rec = self.repo.get("Messages", msg_id)
        if not rec:
            return None
        masked = mask_text(message.text or message.caption or "")
        changes = {"text_masked": masked.text, "edited_at": iso(message.edit_date) if message.edit_date else iso(),
                   "mask_summary": merge_summaries(rec.get("mask_summary", ""), masked.summary), "updated_at": iso()}
        prev_issue = rec.get("issue_id", "")
        if rec.get("state") in LINKED_STATES and prev_issue:
            issue = self.repo.get("Issues", prev_issue)
            if issue and not issue.get("accepted_by"):
                changes.update({"state": MSG_QUEUED, "issue_id": "",
                                "state_reason": f"Изменено автором, повторный анализ (было в {prev_issue})"})
        self.repo.update("Messages", msg_id, changes)
        if changes.get("state") == MSG_QUEUED and prev_issue:
            recount_issue(self.repo, prev_issue)
        return rec

    # ---------------------------------------------------------------- скриншоты
    def _spawn(self, msg_id: str, file_id: str) -> None:
        task = asyncio.create_task(self.process_file(msg_id, file_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def process_file(self, msg_id: str, file_id: str) -> None:
        entry: dict = {"status": "error", "reason": "Скрин не обработан"}
        summary = ""
        async with self._sem:
            try:
                if not self.downloader:
                    raise RuntimeError("загрузка файлов недоступна")
                raw = await self.downloader(file_id)
                res = await asyncio.to_thread(mask_image, raw, self.langs)
                summary = res.summary
                if not res.verified:
                    entry = {"status": "unverified", "reason": res.reason or "Скрин распознан неуверенно"}
                elif not self.uploader:
                    entry = {"status": "unverified", "reason": "Не задан служебный канал — обезличенный скрин не сохранён"}
                else:
                    masked_id = await self.uploader(res.image_bytes, msg_id)
                    entry = {"status": "ok", "file_id": masked_id, "ocr": res.ocr_text_masked,
                             "w": res.width, "h": res.height, "masked": res.summary}
            except Exception as exc:
                log.warning("Скрин %s не обработан: %s", msg_id, exc)
                entry = {"status": "error", "reason": f"Ошибка обработки: {type(exc).__name__}"}
        rec = self.repo.get("Messages", msg_id)
        if not rec:
            return
        screens = jloads(rec.get("screens", ""), [])
        screens.append(entry)
        raw_files = [f for f in jloads(rec.get("raw_files", ""), []) if f != file_id]
        if raw_files:
            status = "pending"
        elif all(s.get("status") == "ok" for s in screens):
            status = "ok"
        else:
            status = "unverified"
        self.repo.update("Messages", msg_id, {
            "screens": jdumps(screens),
            "raw_files": jdumps(raw_files) if raw_files else "",
            "screen_status": status,
            "mask_summary": merge_summaries(rec.get("mask_summary", ""), summary),
            "updated_at": iso(),
        })

    def resume_pending(self) -> int:
        """После перезапуска — дообработать скрины, которые не успели обезличить."""
        n = 0
        for rec in self.repo.rows("Messages"):
            for file_id in jloads(rec.get("raw_files", ""), []):
                self._spawn(rec["msg_id"], file_id)
                n += 1
        return n

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

