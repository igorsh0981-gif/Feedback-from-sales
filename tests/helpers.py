from datetime import timedelta

from app.pipeline.classifier import Analysis
from app.storage.backends import MemoryBackend
from app.storage.repo import Repo
from app.util import iso, now


async def make_repo() -> Repo:
    repo = Repo(MemoryBackend())
    await repo.init()
    repo.insert("Chats", {"chat_id": "-1001", "title": "Android · Ташкент", "default_platform": "Android", "active": "1"})
    repo.insert("Chats", {"chat_id": "-1002", "title": "iOS · Пилот", "default_platform": "iOS", "active": "1"})
    await repo.commit()
    return repo


_counter = {"n": 0}


def add_msg(repo: Repo, text: str, chat="-1001", author="u1", minutes_ago=30, reply_to="", version="",
            platform="", kind="text", screens="") -> dict:
    _counter["n"] += 1
    mid = str(_counter["n"])
    return repo.insert("Messages", {
        "msg_id": f"{chat}:{mid}", "chat_id": chat, "message_id": mid, "reply_to": reply_to,
        "author_id": author, "author_name": author, "sent_at": iso(now() - timedelta(minutes=minutes_ago)),
        "kind": kind, "text_masked": text, "state": "В очереди", "app_version": version, "platform": platform,
        "screens": screens,
    })


class ScriptedClassifier:
    """Возвращает заранее заданные ответы по порядку."""

    name = "scripted"

    def __init__(self, *answers: dict):
        self.answers = list(answers)
        self.inputs = []

    async def classify(self, ep):
        self.inputs.append(ep)
        data = self.answers.pop(0) if self.answers else {"kind": "review", "confidence": 0.2, "reason": "нет ответа"}
        return Analysis.from_dict(data)
