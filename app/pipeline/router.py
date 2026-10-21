"""Маршрутизация результата разбора (раздел 7 шаг 5 и раздел 8 ТЗ)."""
from __future__ import annotations

from collections import Counter

from ..constants import (
    MSG_ACCOUNTED,
    MSG_CONTEXT,
    MSG_FILTERED,
    MSG_REVIEW,
    NO_OWNER,
    NO_PLATFORM,
    REL_REGRESSION,
    REL_SIMILAR,
)
from ..services import (
    audit,
    ensure_operation,
    is_open,
    link_issues,
    max_priority,
    platform_union,
    resolve_issue,
)
from ..storage.repo import Repo
from ..util import compare_versions, iso, parse_version
from .classifier import Analysis
from .episodes import Episode

AI = "AI"
LOW_CONFIDENCE = 0.6


def episode_platform(ep: Episode, analysis: Analysis, chat_default: str) -> str:
    platforms = {m.get("platform") for m in ep.messages if m.get("platform") in ("iOS", "Android")}
    if platforms:
        result = NO_PLATFORM
        for p in platforms:
            result = platform_union(result, p)
        return result
    if analysis.platform and analysis.platform != NO_PLATFORM:
        return analysis.platform
    if chat_default in ("iOS", "Android"):
        return chat_default
    return NO_PLATFORM


def episode_version(ep: Episode, analysis: Analysis) -> str:
    """Версия из профиля участника, затем из текста или скрина."""
    versions = [m.get("app_version") for m in ep.messages if m.get("app_version")]
    if versions:
        return Counter(versions).most_common(1)[0][0]
    return analysis.app_version


def episode_device(ep: Episode, analysis: Analysis) -> str:
    for m in ep.messages:
        if m.get("device"):
            return m["device"]
    return analysis.device


def _set_messages(repo: Repo, ep: Episode, state: str, reason: str, issue_id: str = "") -> None:
    for m in ep.messages:
        repo.update("Messages", m["msg_id"], {
            "state": state, "state_reason": reason, "state_by": AI, "issue_id": issue_id,
            "episode_id": ep.id, "updated_at": iso(),
        })


def _create_issue(repo: Repo, ep: Episode, a: Analysis, platform: str, version: str, device: str) -> dict:
    section, operation = ensure_operation(repo, a.section, a.operation)
    issue_id = repo.next_issue_id()
    rec = repo.insert("Issues", {
        "issue_id": issue_id,
        "created_at": ep.messages[0].get("sent_at") or iso(),
        "updated_at": iso(),
        "type_ai": a.type,
        "type": a.type,
        "section": section,
        "operation": operation,
        "platform": platform,
        "device": device,
        "app_version": version,
        "priority_ai": a.priority,
        "priority": a.priority,
        "status": "Новое",
        "title": a.title or "Обращение участника",
        "summary": a.summary,
        "steps": "\n".join(a.steps),
        "actual": a.actual,
        "expected": a.expected,
        "error_text": a.error_text,
        "reason_ai": a.reason,
        "confidence": f"{a.confidence:.2f}",
        "owner": NO_OWNER,
    })
    audit(repo, AI, issue_id, "Создано", "", f"{a.type} · {section} / {operation}")
    return rec


def apply_analysis(repo: Repo, ep: Episode, a: Analysis, chat_default: str = "") -> tuple[str, str]:
    """Применяет разбор. Возвращает (исход, issue_id)."""
    platform = episode_platform(ep, a, chat_default)
    version = episode_version(ep, a)
    device = episode_device(ep, a)
    match = resolve_issue(repo, a.match_issue) if a.match_issue else None

    if a.kind == "noise":
        _set_messages(repo, ep, MSG_FILTERED, a.reason or "Не относится к работе приложения")
        return "noise", ""

    if a.kind == "review":
        _set_messages(repo, ep, MSG_REVIEW, a.reason or "Не хватает контекста — нужна проверка администратора")
        return "review", ""

    if a.kind == "context":
        target = match or (resolve_issue(repo, ep.reply_issue) if ep.reply_issue else None)
        if target and a.confidence >= 0.5:
            _set_messages(repo, ep, MSG_CONTEXT, a.reason or f"Уточнение к {target['issue_id']}", target["issue_id"])
            _touch(repo, target, platform)
            return "context", target["issue_id"]
        _set_messages(repo, ep, MSG_REVIEW, "Похоже на уточнение, но обращение не найдено. " + (a.reason or ""))
        return "review", ""

    # kind == issue
    if a.confidence < 0.35:
        _set_messages(repo, ep, MSG_REVIEW, "Низкая уверенность классификации. " + (a.reason or ""))
        return "review", ""

    if match and a.relation in ("same", "repeat"):
        if is_open(match):
            _set_messages(repo, ep, MSG_ACCOUNTED, a.reason or f"Повторный сигнал к {match['issue_id']}", match["issue_id"])
            _touch(repo, match, platform)
            return "signal", match["issue_id"]
        if match.get("type") == "Баг" and match.get("status") in ("Устранено", "Закрыто"):
            cmp = compare_versions(version, match.get("fix_version"))
            if cmp is not None and cmp < 0:
                reason = (f"Старая версия: у участника {version}, исправлено в {match.get('fix_version')}. "
                          f"Обращение {match['issue_id']} не переоткрывается — нужно обновить приложение.")
                _set_messages(repo, ep, MSG_ACCOUNTED, reason, match["issue_id"])
                return "old_version", match["issue_id"]
            rec = _create_issue(repo, ep, a, platform, version, device)
            if cmp is not None:
                repo.update("Issues", rec["issue_id"], {
                    "priority": max_priority(rec["priority"], match.get("priority", "")),
                    "reason_ai": (rec["reason_ai"] + f" Регрессия: повтор {match['issue_id']} на версии {version}, "
                                  f"исправление было в {match.get('fix_version')}.").strip(),
                })
                link_issues(repo, rec["issue_id"], match["issue_id"], REL_REGRESSION, AI)
            else:
                link_issues(repo, rec["issue_id"], match["issue_id"], REL_SIMILAR, AI)
            _set_messages(repo, ep, MSG_ACCOUNTED, rec["reason_ai"], rec["issue_id"])
            return "new", rec["issue_id"]
        # Отклонённое, реализованное или отвеченное — новое обращение со ссылкой на прежнее.
        rec = _create_issue(repo, ep, a, platform, version, device)
        link_issues(repo, rec["issue_id"], match["issue_id"], REL_SIMILAR, AI)
        _set_messages(repo, ep, MSG_ACCOUNTED, rec["reason_ai"], rec["issue_id"])
        return "new", rec["issue_id"]

    rec = _create_issue(repo, ep, a, platform, version, device)
    if match and a.relation == "similar":
        link_issues(repo, rec["issue_id"], match["issue_id"], REL_SIMILAR, AI)
    _set_messages(repo, ep, MSG_ACCOUNTED, rec["reason_ai"], rec["issue_id"])
    return "new", rec["issue_id"]


def _touch(repo: Repo, issue: dict, platform: str) -> None:
    changes = {"updated_at": iso()}
    merged = platform_union(issue.get("platform", ""), platform)
    if merged != issue.get("platform"):
        changes["platform"] = merged
    repo.update("Issues", issue["issue_id"], changes)


def learn_profile(repo: Repo, ep: Episode, a: Analysis) -> dict:
    """Профиль участника без регистрации: платформа, устройство и версия из того, что он сам написал или показал.

    Обновляется только эпизод одного автора. Версия меняется, только если новая не старше записанной.
    Возвращает изменённые поля профиля.
    """
    if len(ep.author_ids) != 1:
        return {}
    pid = ep.author_ids[0]
    person = repo.get("Participants", pid)
    if not person:
        return {}
    changes: dict[str, str] = {}
    if a.platform in ("iOS", "Android") and not person.get("platform"):
        changes["platform"] = a.platform
    if a.device and not person.get("device"):
        changes["device"] = a.device
    if a.app_version and parse_version(a.app_version):
        cmp = compare_versions(a.app_version, person.get("app_version"))
        if not person.get("app_version") or (cmp is not None and cmp > 0):
            changes["app_version"] = a.app_version
    if not changes:
        return {}
    changes["updated_at"] = iso()
    repo.update("Participants", pid, changes)
    for m in ep.messages:
        fill = {k: v for k, v in changes.items() if k in ("platform", "device", "app_version") and not m.get(k)}
        if fill:
            repo.update("Messages", m["msg_id"], fill)
            m.update(fill)
    return changes
