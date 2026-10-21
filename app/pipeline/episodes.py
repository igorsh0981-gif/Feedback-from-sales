"""Сборка эпизодов (раздел 2 и 7 ТЗ).

Эпизод — сообщения об одном: цепочка ответов, альбом (склеивается при приёме),
серия сообщений одного автора в одном чате и теме с паузой не больше EPISODE_PAUSE_MIN.
Эпизод готов к анализу, когда в нём нет новых сообщений EPISODE_PAUSE_MIN минут
(ручной запуск админом анализирует всё сразу).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..constants import LINKED_STATES
from ..util import parse_dt

SCREEN_WAIT = timedelta(minutes=15)


@dataclass
class Episode:
    id: str
    messages: list[dict]
    reply_issue: str = ""
    reply_texts: list[str] = field(default_factory=list)

    @property
    def chat_id(self) -> str:
        return self.messages[0]["chat_id"]

    @property
    def first_at(self) -> datetime | None:
        return parse_dt(self.messages[0].get("sent_at", ""))

    @property
    def last_at(self) -> datetime | None:
        return parse_dt(self.messages[-1].get("sent_at", ""))

    @property
    def author_ids(self) -> list[str]:
        out: list[str] = []
        for m in self.messages:
            if m.get("author_id") and m["author_id"] not in out:
                out.append(m["author_id"])
        return out


class _DSU:
    def __init__(self, keys):
        self.p = {k: k for k in keys}

    def find(self, k):
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def build_episodes(
    pending: list[dict],
    all_messages: dict[str, dict],
    pause_min: int,
    now: datetime,
    force: bool = False,
) -> tuple[list[Episode], list[Episode]]:
    """Возвращает (готовые, ожидающие) эпизоды."""
    if not pending:
        return [], []
    pending = sorted(pending, key=lambda m: (m.get("sent_at", ""), m["msg_id"]))
    ids = [m["msg_id"] for m in pending]
    pending_ids = set(ids)
    dsu = _DSU(ids)
    pause = timedelta(minutes=pause_min)

    last_by_stream: dict[tuple, dict] = {}
    for m in pending:
        if m.get("reply_to"):
            target = f"{m['chat_id']}:{m['reply_to']}"
            if target in pending_ids:
                dsu.union(target, m["msg_id"])
        stream = (m.get("chat_id"), m.get("author_id"), m.get("thread_id", ""))
        prev = last_by_stream.get(stream)
        if prev:
            t_prev, t_cur = parse_dt(prev.get("sent_at", "")), parse_dt(m.get("sent_at", ""))
            if t_prev and t_cur and t_cur - t_prev <= pause:
                dsu.union(prev["msg_id"], m["msg_id"])
        last_by_stream[stream] = m

    groups: dict[str, list[dict]] = {}
    for m in pending:
        groups.setdefault(dsu.find(m["msg_id"]), []).append(m)

    ready, waiting = [], []
    for root, msgs in groups.items():
        ep = Episode(id=f"E-{msgs[0]['msg_id']}", messages=msgs)
        for m in msgs:
            if not m.get("reply_to"):
                continue
            target = all_messages.get(f"{m['chat_id']}:{m['reply_to']}")
            if not target or target["msg_id"] in pending_ids:
                continue
            if target.get("text_masked"):
                ep.reply_texts.append(target["text_masked"])
            if not ep.reply_issue and target.get("state") in LINKED_STATES and target.get("issue_id"):
                ep.reply_issue = target["issue_id"]
        last = ep.last_at
        screens_pending = any(
            m.get("screen_status") == "pending" and (parse_dt(m.get("sent_at", "")) or now) > now - SCREEN_WAIT
            for m in msgs
        )
        quiet = last is None or now - last >= pause
        if screens_pending or not (force or quiet):
            waiting.append(ep)
        else:
            ready.append(ep)
    ready.sort(key=lambda e: e.messages[0].get("sent_at", ""))
    return ready, waiting
