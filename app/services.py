"""Операции над обращениями и сообщениями: общие для анализа и дашборда."""
from __future__ import annotations

from datetime import date

from .constants import (
    BOTH_PLATFORMS,
    LINKED_STATES,
    MSG_ACCOUNTED,
    MSG_CONTEXT,
    MSG_FILTERED,
    MSG_QUEUED,
    MSG_REVIEW,
    NO_OWNER,
    NO_PLATFORM,
    OTHER_SECTION,
    PLATFORMS,
    PRIORITIES,
    PRIORITY_RANK,
    REL_MERGED,
    RESOLVED_STATUSES,
    SECTIONS,
    STATUSES,
    TERMINAL_STATUSES,
    TYPES,
    UNKNOWN_OPERATION,
)
from .storage.repo import Repo
from .util import add_working_days, iso, join_list, parse_date, today

EDITABLE_FIELDS = [
    "type", "priority", "status", "owner", "section", "operation", "platform", "device", "app_version",
    "title", "summary", "steps", "actual", "expected", "error_text", "due", "resolved_at", "fix_version",
    "resolution", "reject_reason", "comment", "jira_key", "jira_text",
]
FIELD_NAMES = {
    "type": "Тип", "priority": "Приоритет", "status": "Статус", "owner": "Ответственный", "section": "Раздел",
    "operation": "Операция", "platform": "Платформа", "device": "Устройство", "app_version": "Версия",
    "title": "Заголовок", "summary": "Суть", "steps": "Шаги", "actual": "Факт", "expected": "Ожидание",
    "error_text": "Текст ошибки", "due": "Срок", "resolved_at": "Дата устранения", "fix_version": "Версия исправления",
    "resolution": "Результат", "reject_reason": "Причина отклонения", "comment": "Комментарий",
    "jira_key": "Ключ Jira", "jira_text": "Текст задачи", "accepted_by": "Принял", "accepted_at": "Дата принятия",
    "due_set_by": "Срок поставил", "merged_into": "Объединено с",
    "state": "Состояние", "issue_id": "Обращение", "active": "Активен", "default_platform": "Платформа по умолчанию",
    "value": "Значение",
}
ACCEPT_STATUS = {"Баг": "Принято", "Пожелание": "Оценка", "UX": "Оценка", "Вопрос": "Новое"}


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- журнал
def audit(repo: Repo, actor: str, object_id: str, field: str, old: str, new: str) -> None:
    repo.insert("Audit", {"ts": iso(), "actor": actor, "object_id": object_id, "field": field, "old": old, "new": new})


def audit_changes(repo: Repo, actor: str, object_id: str, old: dict, rec: dict) -> None:
    for field, before in old.items():
        if field in {"updated_at", "jira_text"}:
            if field == "jira_text":
                audit(repo, actor, object_id, FIELD_NAMES[field], "", "изменён")
            continue
        audit(repo, actor, object_id, FIELD_NAMES.get(field, field), before, rec.get(field, ""))


# ---------------------------------------------------------------- справочники
def platform_union(a: str, b: str) -> str:
    vals = {p for p in (a, b) if p and p != NO_PLATFORM}
    if BOTH_PLATFORMS in vals or {"iOS", "Android"} <= vals:
        return BOTH_PLATFORMS
    if vals:
        return next(iter(vals))
    return NO_PLATFORM


def max_priority(a: str, b: str) -> str:
    ra, rb = PRIORITY_RANK.get(a, 9), PRIORITY_RANK.get(b, 9)
    return a if ra <= rb else b


def resolve_issue(repo: Repo, issue_id: str) -> dict | None:
    """Обращение с учётом объединения (merged_into)."""
    seen = set()
    rec = repo.get("Issues", issue_id) if issue_id else None
    while rec and rec.get("merged_into") and rec["issue_id"] not in seen:
        seen.add(rec["issue_id"])
        nxt = repo.get("Issues", rec["merged_into"])
        if not nxt:
            break
        rec = nxt
    return rec


def is_open(rec: dict) -> bool:
    return rec.get("status") not in TERMINAL_STATUSES and not rec.get("merged_into")


def ensure_operation(repo: Repo, section: str, operation: str) -> tuple[str, str]:
    """Раздел и операция из справочника; новая операция добавляется со статусом «Предложена»."""
    section = section if section in SECTIONS else OTHER_SECTION
    operation = (operation or "").strip() or UNKNOWN_OPERATION
    if operation == UNKNOWN_OPERATION:
        return section, operation
    rec = repo.get("Operations", section, operation)
    if rec:
        if rec.get("merged_into"):
            return section, rec["merged_into"]
        return section, operation
    # Совпадение без учёта регистра
    for op in repo.rows("Operations"):
        if op["section"] == section and op["operation"].lower() == operation.lower():
            return section, op.get("merged_into") or op["operation"]
    repo.insert("Operations", {
        "section": section, "operation": operation, "keywords": "",
        "status": "Предложена", "merged_into": "", "created_at": iso(),
    })
    return section, operation


# ---------------------------------------------------------------- сроки
def next_release_date(repo: Repo, after: date, platform: str = "") -> date | None:
    best = None
    for rel in repo.rows("Releases"):
        d = parse_date(rel.get("released_at", ""))
        if not d or d <= after:
            continue
        if platform in ("iOS", "Android") and rel.get("platform") not in ("", platform, BOTH_PLATFORMS):
            continue
        best = d if best is None or d < best else best
    return best


def default_due(repo: Repo, priority: str, base: date | None = None, platform: str = "") -> str:
    base = base or today()
    if priority == "Критичный":
        return add_working_days(base, 1).isoformat()
    if priority == "Высокий":
        return add_working_days(base, 3).isoformat()
    if priority == "Средний":
        d = next_release_date(repo, base, platform)
        return d.isoformat() if d else ""
    return ""


def is_overdue(rec: dict) -> bool:
    due = parse_date(rec.get("due", ""))
    return bool(due and due < today() and rec.get("status") not in TERMINAL_STATUSES)


def in_review(rec: dict) -> bool:
    return rec.get("status") == "Новое" and not rec.get("accepted_by") and not rec.get("merged_into")


# ---------------------------------------------------------------- счётчики
def also_issues(m: dict) -> list[str]:
    """Дополнительные обращения сообщения, в котором несколько тем."""
    return [x for x in (m.get("also_issues") or "").split(",") if x]


def message_in_issue(m: dict, issue_id: str) -> bool:
    return m.get("state") in LINKED_STATES and (m.get("issue_id") == issue_id or issue_id in also_issues(m))


def recount_issue(repo: Repo, issue_id: str) -> None:
    rec = repo.get("Issues", issue_id)
    if not rec:
        return
    ids = {issue_id} | {c["issue_id"] for c in repo.rows("Issues") if c.get("merged_into") == issue_id}
    msgs = [m for m in repo.rows("Messages") if any(message_in_issue(m, i) for i in ids)]
    authors = join_list(m.get("author_id", "") for m in sorted(msgs, key=lambda m: m.get("sent_at", "")))
    chats = join_list(m.get("chat_id", "") for m in msgs)
    unverified = any(m.get("screen_status") == "unverified" for m in msgs)
    repo.update("Issues", issue_id, {
        "msg_count": str(len(msgs)),
        "participant_count": str(len([a for a in authors.split(",") if a])),
        "authors": authors,
        "chats": chats,
        "screen_unverified": "1" if unverified else "",
    })


# ---------------------------------------------------------------- действия админа
def validate_and_save(repo: Repo, issue_id: str, data: dict, actor: str, accept: bool = False) -> dict:
    rec = repo.get("Issues", issue_id)
    if not rec:
        raise ValidationError([f"Обращение {issue_id} не найдено"])
    if rec.get("merged_into"):
        raise ValidationError([f"{issue_id} объединено с {rec['merged_into']}; правьте основное обращение"])
    new = dict(rec)
    for field in EDITABLE_FIELDS:
        if field in data and data[field] is not None:
            new[field] = str(data[field]).strip() if field not in {"steps", "jira_text", "resolution", "comment", "summary"} else str(data[field])
    errors: list[str] = []
    t = new.get("type")
    if t not in TYPES:
        errors.append("Укажите тип обращения")
    if new.get("priority") not in PRIORITIES:
        errors.append("Укажите приоритет")
    if new.get("platform") not in PLATFORMS:
        errors.append("Недопустимая платформа")
    if new.get("section") not in SECTIONS:
        errors.append("Недопустимый раздел")
    if t in STATUSES and new.get("status") not in STATUSES[t]:
        if new.get("type") != rec.get("type") and new.get("status") == rec.get("status"):
            # Сменили тип — подбираем ближайший допустимый статус.
            new["status"] = "Новое" if rec.get("status") == "Новое" else ACCEPT_STATUS.get(t, "Новое")
        else:
            errors.append(f"Статус «{new.get('status')}» не подходит для типа «{t}»")
    new.setdefault("owner", NO_OWNER)
    if not new.get("owner"):
        new["owner"] = NO_OWNER

    # Принятое обращение не возвращается в «Новое» при смене типа (иначе выпадает из очереди и висит без статуса).
    if rec.get("accepted_by") and new.get("status") == "Новое" and t != "Вопрос":
        new["status"] = ACCEPT_STATUS.get(t, "Новое")

    now_iso = iso()
    if accept and not new.get("accepted_by"):
        new["accepted_by"] = actor
        new["accepted_at"] = now_iso
        if new.get("status") == "Новое":
            new["status"] = ACCEPT_STATUS.get(t, "Новое")

    accepted_date = parse_date(new.get("accepted_at", "")) or today()
    status = new.get("status", "")
    due = parse_date(new.get("due", ""))
    if new.get("due") and not due:
        errors.append("Срок указан в неверном формате")
    if t == "Баг" and new.get("accepted_by") and status not in {"Новое", "Уточнение", "Отклонено"} | RESOLVED_STATUSES:
        if not due and new.get("priority") != "Низкий":
            errors.append("Для принятого бага укажите срок устранения")
    if due and (new.get("due") != rec.get("due") or accept) and due < accepted_date:
        errors.append("Срок не может быть раньше даты принятия")
    if status in RESOLVED_STATUSES:
        resolved = parse_date(new.get("resolved_at", ""))
        if not resolved:
            errors.append("Укажите дату устранения")
        elif resolved > today():
            errors.append("Дата устранения не может быть в будущем")
        if t in {"Баг", "Пожелание", "UX"} and not new.get("fix_version"):
            errors.append("Укажите версию исправления")
        if not new.get("resolution", "").strip():
            errors.append("Опишите результат")
        if new.get("owner") in ("", NO_OWNER):
            errors.append("Назначьте ответственного")
    if status == "Отклонено" and not new.get("reject_reason", "").strip():
        errors.append("Укажите причину отклонения")
    if errors:
        raise ValidationError(errors)

    if new.get("due") != rec.get("due"):
        new["due_set_by"] = actor if new.get("due") else ""
    if status not in RESOLVED_STATUSES and rec.get("status") in RESOLVED_STATUSES and status != rec.get("status"):
        # Вернули в работу — дата и результат устранения остаются в журнале, в карточке очищаются.
        new["resolved_at"] = ""
    changes = {k: v for k, v in new.items() if rec.get(k, "") != v}
    if not changes:
        return rec
    changes["updated_at"] = now_iso
    old = repo.update("Issues", issue_id, changes)
    audit_changes(repo, actor, issue_id, old, repo.get("Issues", issue_id))
    return repo.get("Issues", issue_id)


def active_links(repo: Repo) -> list[dict]:
    """Связи без снятых администратором."""
    return [l for l in repo.rows("Links") if not l.get("removed_at")]


def link_issues(repo: Repo, issue_id: str, related_id: str, relation: str, actor: str) -> None:
    if issue_id == related_id:
        return
    existing = repo.get("Links", issue_id, related_id, relation)
    if existing:
        if existing.get("removed_at"):
            repo.update("Links", Repo.make_key(issue_id, related_id, relation), {"removed_by": "", "removed_at": ""})
        return
    repo.insert("Links", {"issue_id": issue_id, "related_id": related_id, "relation": relation,
                          "created_by": actor, "created_at": iso()})


def merge_issues(repo: Repo, source_id: str, target_id: str, actor: str) -> dict:
    src = repo.get("Issues", source_id)
    dst = resolve_issue(repo, target_id)
    if not src or not dst:
        raise ValidationError(["Обращение не найдено"])
    if src["issue_id"] == dst["issue_id"]:
        raise ValidationError(["Нельзя объединить обращение с самим собой"])
    if not is_open(src) or not is_open(dst):
        raise ValidationError(["Объединять можно только открытые обращения; закрытые остаются историей"])
    old = repo.update("Issues", source_id, {"merged_into": dst["issue_id"], "updated_at": iso()})
    audit_changes(repo, actor, source_id, old, repo.get("Issues", source_id))
    link_issues(repo, dst["issue_id"], source_id, REL_MERGED, actor)
    # Ссылки сообщений на объединённое обращение остаются; счётчики основного учитывают их.
    changes = {
        "platform": platform_union(dst.get("platform", ""), src.get("platform", "")),
        "priority": max_priority(dst.get("priority", ""), src.get("priority", "")),
        "updated_at": iso(),
    }
    old = repo.update("Issues", dst["issue_id"], changes)
    audit_changes(repo, actor, dst["issue_id"], old, repo.get("Issues", dst["issue_id"]))
    audit(repo, actor, dst["issue_id"], "Объединение", "", f"добавлено {source_id}")
    recount_issue(repo, dst["issue_id"])
    return repo.get("Issues", dst["issue_id"])


def _remove_link(repo: Repo, a: str, b: str, relation: str, actor: str) -> bool:
    removed = False
    for x, y in ((a, b), (b, a)):
        link = repo.get("Links", x, y, relation)
        if link and not link.get("removed_at"):
            repo.update("Links", Repo.make_key(x, y, relation), {"removed_by": actor, "removed_at": iso()})
            removed = True
    return removed


def unmerge_issues(repo: Repo, a: str, b: str, actor: str) -> dict:
    """Отменяет ошибочное объединение: присоединённое обращение снова самостоятельное, сообщения возвращаются к нему."""
    ra, rb = repo.get("Issues", a), repo.get("Issues", b)
    if not ra or not rb:
        raise ValidationError(["Обращение не найдено"])
    if ra.get("merged_into") == b:
        src, dst = ra, rb
    elif rb.get("merged_into") == a:
        src, dst = rb, ra
    else:
        raise ValidationError([f"{a} и {b} не объединены"])
    repo.update("Issues", src["issue_id"], {"merged_into": "", "updated_at": iso()})
    _remove_link(repo, dst["issue_id"], src["issue_id"], REL_MERGED, actor)
    audit(repo, actor, src["issue_id"], FIELD_NAMES["merged_into"], dst["issue_id"], "")
    audit(repo, actor, dst["issue_id"], "Разъединение", "", f"выделено {src['issue_id']}")
    recount_issue(repo, src["issue_id"])
    recount_issue(repo, dst["issue_id"])
    return repo.get("Issues", src["issue_id"])


def unlink_issues(repo: Repo, a: str, b: str, relation: str, actor: str) -> None:
    if relation == REL_MERGED:
        unmerge_issues(repo, a, b, actor)
        return
    if not _remove_link(repo, a, b, relation, actor):
        raise ValidationError(["Связь не найдена"])
    audit(repo, actor, a, "Связь снята", f"{relation}: {b}", "")


def split_message(repo: Repo, msg_id: str, from_issue: str, actor: str) -> dict:
    """Выносит сообщение из обращения в новое — с теми же типом, разделом и приоритетом, на утверждение."""
    msg = repo.get("Messages", msg_id)
    src = repo.get("Issues", from_issue)
    if not msg or not src:
        raise ValidationError(["Сообщение или обращение не найдено"])
    extra = also_issues(msg)
    if msg.get("issue_id") != from_issue and from_issue not in extra:
        raise ValidationError([f"Сообщение не относится к {from_issue}"])
    linked = [m for m in repo.rows("Messages") if message_in_issue(m, from_issue)]
    if len(linked) < 2:
        raise ValidationError(["В обращении одно сообщение — выносить нечего"])
    new_id = repo.next_issue_id()
    text = msg.get("text_masked", "")
    repo.insert("Issues", {
        "issue_id": new_id, "created_at": msg.get("sent_at") or iso(), "updated_at": iso(),
        "type_ai": "", "type": src.get("type", "Вопрос"), "section": src.get("section", OTHER_SECTION),
        "operation": src.get("operation", UNKNOWN_OPERATION), "platform": msg.get("platform") or src.get("platform") or NO_PLATFORM,
        "device": msg.get("device", ""), "app_version": msg.get("app_version", ""), "priority_ai": "",
        "priority": src.get("priority", "Средний"), "status": "Новое",
        "title": (text.splitlines() or [""])[0][:80] or f"Выделено из {from_issue}", "summary": text,
        "reason_ai": f"Администратор выделил сообщение из {from_issue}. Проверьте тип, раздел и заголовок.",
        "confidence": "", "owner": NO_OWNER,
    })
    if msg.get("issue_id") == from_issue:
        repo.update("Messages", msg_id, {"issue_id": new_id, "state_by": actor, "updated_at": iso(),
                                         "state_reason": f"Выделено из {from_issue}"})
    else:
        repo.update("Messages", msg_id, {"also_issues": ",".join(new_id if x == from_issue else x for x in extra),
                                         "updated_at": iso()})
    audit(repo, actor, new_id, "Создано", "", f"выделено из {from_issue} (сообщение {msg_id})")
    audit(repo, actor, from_issue, "Выделено сообщение", msg_id, new_id)
    recount_issue(repo, from_issue)
    recount_issue(repo, new_id)
    return repo.get("Issues", new_id)


# ---------------------------------------------------------------- сообщения
def set_message_state(repo: Repo, msg_id: str, state: str, reason: str, actor: str, issue_id: str = "") -> dict:
    msg = repo.get("Messages", msg_id)
    if not msg:
        raise ValidationError([f"Сообщение {msg_id} не найдено"])
    prev = {msg.get("issue_id", "")} | set(also_issues(msg))
    old = repo.update("Messages", msg_id, {
        "state": state, "state_reason": reason, "state_by": actor, "issue_id": issue_id, "also_issues": "",
        "updated_at": iso(),
    })
    for field in ("state", "issue_id"):
        if field in old:
            audit(repo, actor, msg_id, FIELD_NAMES[field], old[field], msg.get(field, ""))
    for iid in (prev | {issue_id}) - {""}:
        recount_issue(repo, iid)
    return msg


def message_action(repo: Repo, msg_id: str, action: str, actor: str, reason: str = "", issue_id: str = "") -> dict:
    msg = repo.get("Messages", msg_id)
    if not msg:
        raise ValidationError([f"Сообщение {msg_id} не найдено"])
    if action == "reanalyze":
        return set_message_state(repo, msg_id, MSG_QUEUED, "Возвращено администратором в анализ", actor)
    if action == "review":
        return set_message_state(repo, msg_id, MSG_REVIEW, reason or "Возвращено администратором на проверку", actor)
    if action == "exclude":
        if not reason.strip():
            raise ValidationError(["Укажите, почему сообщение не относится к обратной связи"])
        return set_message_state(repo, msg_id, MSG_FILTERED, reason.strip(), actor)
    if action == "context":
        target = resolve_issue(repo, issue_id)
        if not target:
            raise ValidationError(["Выберите обращение"])
        return set_message_state(repo, msg_id, MSG_CONTEXT, "Администратор сохранил сообщение как уточнение", actor, target["issue_id"])
    if action == "attach":
        target = resolve_issue(repo, issue_id)
        if not target:
            raise ValidationError(["Выберите обращение"])
        return set_message_state(repo, msg_id, MSG_ACCOUNTED, "Администратор добавил сигнал к обращению", actor, target["issue_id"])
    if action == "create":
        new_id = repo.next_issue_id()
        text = msg.get("text_masked", "")
        repo.insert("Issues", {
            "issue_id": new_id, "created_at": msg.get("sent_at") or iso(), "updated_at": iso(),
            "type_ai": "", "type": "Вопрос", "section": OTHER_SECTION, "operation": UNKNOWN_OPERATION,
            "platform": msg.get("platform") or NO_PLATFORM, "device": msg.get("device", ""),
            "app_version": msg.get("app_version", ""), "priority_ai": "", "priority": "Средний",
            "status": "Новое", "title": (text.splitlines() or ["Обращение из сообщения"])[0][:80] or "Обращение из сообщения",
            "summary": text, "reason_ai": "Создано администратором из сообщения. Определите тип и раздел.",
            "confidence": "", "owner": NO_OWNER,
        })
        audit(repo, actor, new_id, "Создано", "", f"из сообщения {msg_id}")
        set_message_state(repo, msg_id, MSG_ACCOUNTED, "Администратор создал обращение", actor, new_id)
        return repo.get("Issues", new_id)
    raise ValidationError([f"Неизвестное действие {action}"])
