"""Сквозной прогон обработчиков бота через Dispatcher с подменённой сессией Telegram."""
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Chat, Message, Update

from app.bot.collector import Collector
from app.bot.handlers import LoginTokens, build_router
from app.config import Config
from app.pipeline.analyzer import Analyzer
from app.pipeline.classifier import HeuristicClassifier
from app.storage.backends import MemoryBackend
from app.storage.repo import Repo

ADMIN = 1
USER = 2


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=999, date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self):
        pass


def upd(n, chat_id, chat_type, user, text=None, data=None):
    chat = {"id": chat_id, "type": chat_type, "title": "Android · Ташкент"} if chat_type != "private" else {"id": chat_id, "type": "private"}
    frm = {"id": user, "is_bot": False, "first_name": "Игорь" if user == ADMIN else "Азиз"}
    if data:
        return Update.model_validate({"update_id": n, "callback_query": {
            "id": str(n), "from": frm, "chat_instance": "c", "data": data,
            "message": {"message_id": 5, "date": datetime.now(timezone.utc), "chat": chat, "text": "x"}}})
    return Update.model_validate({"update_id": n, "message": {
        "message_id": n, "date": datetime.now(timezone.utc), "chat": chat, "from": frm, "text": text}})


async def test_connect_collect_register_login():
    repo = Repo(MemoryBackend())
    await repo.init()
    cfg = Config(admin_ids={ADMIN}, dashboard_url="https://fp.example")
    tokens = LoginTokens()
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_router(repo, cfg, Collector(repo, "eng", None, None), Analyzer(repo, HeuristicClassifier()), tokens))

    # не-админ не может подключить чат; сообщения неподключённого чата не собираются
    await dp.feed_update(bot, upd(1, -100500, "supergroup", USER, "/connect"))
    await dp.feed_update(bot, upd(2, -100500, "supergroup", USER, "Не проходит перевод"))
    assert not repo.get("Chats", "-100500") and not repo.rows("Messages")

    await dp.feed_update(bot, upd(3, -100500, "supergroup", ADMIN, "/connect"))
    assert repo.get("Chats", "-100500")["active"] == "1"
    await dp.feed_update(bot, upd(4, -100500, "supergroup", USER, "Не проходит перевод, тел +998 90 123 45 67"))
    rec = repo.get("Messages", "-100500:4")
    assert rec and "123 45" not in rec["text_masked"] and rec["state"] == "В очереди"

    # регистрация участника
    await dp.feed_update(bot, upd(5, USER, "private", USER, "/start"))
    await dp.feed_update(bot, upd(6, USER, "private", USER, data="reg:Android"))
    await dp.feed_update(bot, upd(7, USER, "private", USER, "Samsung A54"))
    await dp.feed_update(bot, upd(8, USER, "private", USER, "четыре"))
    await dp.feed_update(bot, upd(9, USER, "private", USER, "4.2.0"))
    p = repo.get("Participants", str(USER))
    assert (p["platform"], p["device"], p["app_version"]) == ("Android", "Samsung A54", "4.2.0")
    await dp.feed_update(bot, upd(10, USER, "private", USER, "/version 4.2.1"))
    assert repo.get("Participants", str(USER))["app_version"] == "4.2.1"
    await dp.feed_update(bot, upd(11, -100500, "supergroup", USER, "Опять не проходит"))
    assert repo.get("Messages", "-100500:11")["app_version"] == "4.2.1"

    # вход админа по одноразовой ссылке
    await dp.feed_update(bot, upd(12, USER, "private", USER, "/login"))
    await dp.feed_update(bot, upd(13, ADMIN, "private", ADMIN, "/login"))
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert any("только администраторам" in t for t in texts)
    link = next(t for t in texts if "/auth/link?t=" in t)
    token = link.split("t=")[1].strip()
    assert tokens.consume(token) == (ADMIN, "Игорь")
    assert tokens.consume(token) is None
    assert any("Не похоже на номер версии" in t for t in texts)
