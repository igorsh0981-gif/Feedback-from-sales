"""Служебные операции с данными (по запросу администратора)."""
from __future__ import annotations

import logging

from .services import also_issues, recount_issue
from .util import split_list
from .storage.repo import Repo

log = logging.getLogger(__name__)


async def purge_chat(repo: Repo, chat_id: str) -> dict:
    """Удаляет сообщения чата и обращения, у которых все сообщения из этого чата (с их связями и журналом).

    Обращения, где есть сообщения и из других чатов, остаются — только пересчитываются.
    Сам чат остаётся подключённым.
    """
    chat_id = str(chat_id)
    msgs_all = repo.rows("Messages")
    msg_ids = {m["msg_id"] for m in msgs_all if m.get("chat_id") == chat_id}
    issues = {r["issue_id"]: r for r in repo.rows("Issues")}

    def family(iid: str) -> set[str]:
        out, stack = {iid}, [iid]
        while stack:
            cur = stack.pop()
            for r in issues.values():
                if r.get("merged_into") == cur and r["issue_id"] not in out:
                    out.add(r["issue_id"])
                    stack.append(r["issue_id"])
        return out

    doomed: set[str] = set()
    keep: set[str] = set()
    for iid, r in issues.items():
        fam = family(iid)
        linked = [m for m in msgs_all if m.get("issue_id") in fam or fam & set(also_issues(m))]
        if linked:
            if all(m.get("chat_id") == chat_id for m in linked):
                doomed.add(iid)
            elif any(m.get("chat_id") == chat_id for m in linked):
                keep.add(iid)
        elif split_list(r.get("chats", "")) == [chat_id]:
            doomed.add(iid)
    keep -= doomed
    objects = doomed | msg_ids

    removed = await repo.delete_where({
        "Messages": lambda r: r.get("chat_id") == chat_id,
        "Issues": lambda r: r.get("issue_id") in doomed,
        "Links": lambda r: r.get("issue_id") in doomed or r.get("related_id") in doomed,
        "Audit": lambda r: r.get("object_id") in objects,
    })
    # Обращения, присоединённые к удалённым, но с сообщениями из других чатов, снова самостоятельные.
    # (удалённое основное обращение не должно утянуть за собой чужие сообщения)
    for r in repo.rows("Issues"):
        if r.get("merged_into") in doomed:
            repo.update("Issues", r["issue_id"], {"merged_into": ""})
            keep.add(r["issue_id"])
    for iid in keep:
        recount_issue(repo, iid)
    await repo.commit()
    result = {"chat": chat_id, "issues_removed": sorted(doomed), "issues_recounted": sorted(keep), **removed}
    log.info("Очистка чата %s: %s", chat_id, result)
    return result
