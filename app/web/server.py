"""Дашборд: API и статика (раздел 11 ТЗ).

Просмотр — всем, у кого есть ссылка (опционально — с VIEW_TOKEN).
Действия, меняющие данные, — только админу после входа.
"""
from __future__ import annotations

import asyncio
import logging
import os
from html import escape as html_escape
from collections import OrderedDict
from urllib.parse import unquote
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..config import Config
from ..constants import (
    LINKED_STATES,
    MSG_QUEUED,
    OWNERS,
    PLATFORMS,
    PRIORITIES,
    REJECT_REASONS,
    SECTIONS,
    STATUSES,
    TYPES,
)
from ..pipeline.analyzer import Analyzer
from ..pipeline.jira_text import build_jira_text
from ..services import (
    FIELD_NAMES,
    ValidationError,
    active_links,
    also_issues,
    audit,
    audit_changes,
    default_due,
    in_review,
    is_overdue,
    merge_issues,
    message_action,
    split_message,
    unlink_issues,
    validate_and_save,
)
from ..storage.repo import Repo
from ..util import iso, jloads, join_list, now, parse_date, parse_dt, parse_version, split_list, to_float, to_int
from .auth import SESSION_COOKIE, VIEW_COOKIE, read_session, sign_session, verify_telegram_login, view_cookie_value

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _build_id() -> str:
    """Версия интерфейса — хэш статики: меняется с каждой выкладкой, по ней страница понимает, что её код устарел."""
    import hashlib

    h = hashlib.sha256()
    for name in ("index.html", "app.js", "app.css"):
        h.update((STATIC / name).read_bytes())
    return h.hexdigest()[:12]


BUILD = _build_id()
DEMO_SCREENS = Path(__file__).resolve().parents[2] / "demo" / "screens"


@dataclass
class Health:
    status: dict[str, str] = field(default_factory=lambda: {"sheets": "—", "claude": "—", "telegram": "—"})
    checked_at: str = ""


@dataclass
class AppContext:
    repo: Repo
    cfg: Config
    analyzer: Analyzer
    tokens: Any = None  # LoginTokens
    image_fetcher: Callable[[str], Awaitable[bytes]] | None = None
    release_notifier: Callable[[str, str], Awaitable[int]] | None = None
    is_admin: Callable[[int], bool] | None = None
    bot_username: str = ""
    health: Health = field(default_factory=Health)
    classifier_name: str = ""
    image_cache: "OrderedDict[str, bytes]" = field(default_factory=OrderedDict)
    run_task: asyncio.Task | None = None


CTX = web.AppKey("ctx", AppContext)


# ---------------------------------------------------------------- представления
def _chat_titles(repo: Repo) -> dict[str, str]:
    return {c["chat_id"]: c.get("title") or c["chat_id"] for c in repo.rows("Chats")}


def message_view(m: dict, titles: dict[str, str]) -> dict:
    screens = []
    for idx, s in enumerate(jloads(m.get("screens", ""), [])):
        screens.append({"idx": idx, "status": s.get("status", ""), "reason": s.get("reason", ""),
                        "masked": s.get("masked", "")})
    for _ in jloads(m.get("raw_files", ""), []):
        screens.append({"idx": -1, "status": "pending", "reason": "Скрин обезличивается", "masked": ""})
    return {
        "msg_id": m["msg_id"], "chat_id": m.get("chat_id", ""), "chat": titles.get(m.get("chat_id", ""), m.get("chat_title", "")),
        "author_id": m.get("author_id", ""), "author": m.get("author_name", ""), "sent_at": m.get("sent_at", ""),
        "kind": m.get("kind", ""), "text": m.get("text_masked", ""), "state": m.get("state", ""),
        "reason": m.get("state_reason", ""), "by": m.get("state_by", ""), "issue_id": m.get("issue_id", ""),
        "also": also_issues(m),
        "link": m.get("link", ""), "screens": screens, "mask_summary": m.get("mask_summary", ""),
        "platform": m.get("platform", ""), "device": m.get("device", ""), "app_version": m.get("app_version", ""),
        "edited": bool(m.get("edited_at")),
    }


def _messages_by_issue(repo: Repo) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    merged = {r["issue_id"]: r["merged_into"] for r in repo.rows("Issues") if r.get("merged_into")}
    for m in repo.rows("Messages"):
        if m.get("state") not in LINKED_STATES or not m.get("issue_id"):
            continue
        for iid in [m["issue_id"]] + also_issues(m):
            out.setdefault(iid, []).append(m)
            seen = {iid}
            while iid in merged and merged[iid] not in seen:
                iid = merged[iid]
                seen.add(iid)
                if m not in out.setdefault(iid, []):
                    out[iid].append(m)
    for msgs in out.values():
        msgs.sort(key=lambda m: m.get("sent_at", ""))
    return out


def _participants(repo: Repo) -> dict[str, dict]:
    return {p["telegram_id"]: p for p in repo.rows("Participants")}


def issue_view(repo: Repo, rec: dict, msgs: list[dict], titles: dict[str, str], people: dict[str, dict],
               links: list[dict]) -> dict:
    reporters, seen = [], {}
    for m in msgs:
        aid = m.get("author_id", "")
        if aid in seen:
            seen[aid]["messages"] += 1
            continue
        p = people.get(aid, {})
        item = {
            "author_id": aid, "name": m.get("author_name") or p.get("name", ""), "username": p.get("username", ""),
            "chat": titles.get(m.get("chat_id", ""), ""), "chat_id": m.get("chat_id", ""), "sent_at": m.get("sent_at", ""),
            "msg_id": m["msg_id"], "link": m.get("link", ""), "role": "initiator" if not reporters else "signal",
            "state": m.get("state", ""), "messages": 1,
        }
        seen[aid] = item
        reporters.append(item)
    screens = []
    for m in msgs:
        for idx, s in enumerate(jloads(m.get("screens", ""), [])):
            screens.append({"msg_id": m["msg_id"], "idx": idx, "status": s.get("status", ""), "reason": s.get("reason", "")})
    related = []
    for link in links:
        if link["issue_id"] == rec["issue_id"]:
            other = link["related_id"]
        elif link["related_id"] == rec["issue_id"]:
            other = link["issue_id"]
        else:
            continue
        o = repo.get("Issues", other)
        if not o:
            continue
        related.append({
            "issue_id": other, "relation": link["relation"], "direction": "out" if link["issue_id"] == rec["issue_id"] else "in",
            "title": o.get("title", ""), "status": o.get("status", ""), "created_at": o.get("created_at", ""),
            "resolved_at": o.get("resolved_at", ""), "fix_version": o.get("fix_version", ""),
            "resolution": o.get("resolution", ""), "type": o.get("type", ""),
        })
    view = {k: rec.get(k, "") for k in (
        "issue_id", "created_at", "updated_at", "type_ai", "type", "section", "operation", "platform", "device",
        "app_version", "priority_ai", "priority", "status", "title", "summary", "steps", "actual", "expected",
        "error_text", "reason_ai", "owner", "accepted_by", "accepted_at", "due", "due_set_by", "resolved_at",
        "fix_version", "resolution", "reject_reason", "jira_key", "comment", "merged_into",
    )}
    view.update({
        "confidence": to_float(rec.get("confidence"), 0.0),
        "msg_count": to_int(rec.get("msg_count")),
        "participant_count": to_int(rec.get("participant_count")),
        # Источник — по фактически привязанным сообщениям; сохранённое поле — запасной вариант.
        "chats": split_list(join_list(m.get("chat_id", "") for m in msgs)) if msgs else split_list(rec.get("chats", "")),
        "review": in_review(rec),
        "overdue": is_overdue(rec),
        "reporters": reporters,
        "screens": screens,
        "screen_unverified": rec.get("screen_unverified") == "1",
        "related": related,
        "has_jira_text": bool(rec.get("jira_text")),
        "first_text": msgs[0].get("text_masked", "") if msgs else rec.get("summary", ""),
    })
    return view


def is_owner(ctx: AppContext, admin: dict | None) -> bool:
    """Владелец — админ из ADMIN_IDS, вошедший через бота (/login или Telegram). Вход по общей ссылке не считается."""
    if not admin or admin.get("link"):
        return False
    uid = admin.get("uid")
    return (isinstance(uid, int) and uid in ctx.cfg.admin_ids) or (ctx.cfg.demo_mode and uid == -1)


def share_link(ctx: AppContext, admin: dict | None) -> dict | None:
    """Ссылка на дашборд для рассылки участникам — видна только владельцу."""
    if not is_owner(ctx, admin):
        return None
    query = f"/?k={ctx.cfg.view_token}" if ctx.cfg.view_token else "/"
    return {"base": ctx.cfg.dashboard_url, "path": query, "edit": ctx.cfg.open_edit,
            "protected": bool(ctx.cfg.view_token)}


def state_payload(ctx: AppContext, admin: dict | None) -> dict:
    repo = ctx.repo
    titles = _chat_titles(repo)
    people = _participants(repo)
    by_issue = _messages_by_issue(repo)
    links = active_links(repo)
    issues = [issue_view(repo, r, by_issue.get(r["issue_id"], []), titles, people, links) for r in repo.rows("Issues")]
    cutoff = iso(now() - timedelta(days=30))
    msgs = [message_view(m, titles) for m in repo.rows("Messages")
            if m.get("sent_at", "") >= cutoff or m.get("state") not in LINKED_STATES]
    msgs.sort(key=lambda m: m["sent_at"], reverse=True)
    chat_stats: dict[str, dict] = {}
    for m in repo.rows("Messages"):
        st = chat_stats.setdefault(m.get("chat_id", ""), {"messages": 0, "last": "", "authors": set()})
        st["messages"] += 1
        st["last"] = max(st["last"], m.get("sent_at", ""))
        if m.get("author_id"):
            st["authors"].add(m["author_id"])
    chats = []
    for c in repo.rows("Chats"):
        st = chat_stats.get(c["chat_id"], {"messages": 0, "last": "", "authors": set()})
        chats.append({**{k: c.get(k, "") for k in ("chat_id", "title", "default_platform", "active", "added_at", "removed_at")},
                      "messages": st["messages"], "last_message_at": st["last"], "participants": len(st["authors"])})
    audit_rows = repo.rows("Audit")[-400:][::-1]
    runs = sorted(repo.rows("Runs"), key=lambda r: r.get("started_at", ""), reverse=True)[:40]
    an = ctx.analyzer
    last = an.last_run
    last_run = None
    if last:
        last_run = {"finished_at": last.finished_at, "new_issues": last.new_issues, "signals": last.signals,
                    "filtered": last.filtered, "review": last.review, "errors": last.errors, "waiting": last.waiting}
    elif runs:
        r = runs[0]
        last_run = {"finished_at": r.get("finished_at", ""), "new_issues": to_int(r.get("new_issues")),
                    "signals": to_int(r.get("signals")), "filtered": to_int(r.get("filtered")),
                    "review": to_int(r.get("review")), "errors": to_int(r.get("errors")), "waiting": 0}
    return {
        "me": {"admin": bool(admin), "name": (admin or {}).get("name", ""), "uid": (admin or {}).get("uid"),
               "link": bool((admin or {}).get("link")), "share": share_link(ctx, admin)},
        "meta": {
            "build": BUILD,
            "demo": ctx.cfg.demo_mode, "bot_username": ctx.bot_username, "classifier": ctx.classifier_name,
            "running": an.running, "last_run": last_run,
            "next_run_at": iso(an.next_run_at) if an.next_run_at else "",
            "auto": repo.setting_on("auto_analysis"), "interval_min": an.interval_min, "pause_min": an.pause_min,
            "calls_today": an.calls_today(), "daily_limit": an.daily_limit, "health": ctx.health.status,
            "health_checked_at": ctx.health.checked_at, "queued": sum(1 for m in repo.rows("Messages") if m.get("state") == MSG_QUEUED),
            "now": iso(), "login_widget": bool(ctx.bot_username and not ctx.cfg.demo_mode and os.getenv("TELEGRAM_LOGIN_WIDGET") == "1"),
            "pending_writes": repo.pending_changes,
        },
        "options": {
            "types": TYPES, "priorities": PRIORITIES, "platforms": PLATFORMS, "owners": OWNERS,
            "sections": SECTIONS, "statuses": STATUSES, "reject_reasons": REJECT_REASONS,
        },
        "chats": chats,
        "operations": repo.rows("Operations"),
        "releases": sorted(repo.rows("Releases"), key=lambda r: r.get("released_at", ""), reverse=True),
        "participants": len(people),
        "admins": [{"telegram_id": a.get("telegram_id"), "name": a.get("name")} for a in repo.rows("Admins")]
        + [{"telegram_id": str(i), "name": "из ADMIN_IDS"} for i in sorted(ctx.cfg.admin_ids)],
        "settings": {s["key"]: s["value"] for s in repo.rows("Settings")},
        "issues": issues,
        "messages": msgs,
        "runs": runs,
        "audit": audit_rows,
    }


# ---------------------------------------------------------------- middleware
def _session(request: web.Request) -> dict | None:
    ctx = request.app[CTX]
    return read_session(ctx.cfg.session_secret, request.cookies.get(SESSION_COOKIE))


def _admin(request: web.Request) -> dict | None:
    ctx = request.app[CTX]
    data = _session(request)
    if not data:
        if ctx.cfg.open_edit:
            # Режим «редактирует любой по ссылке»: имя берётся из заголовка, чтобы журнал показывал, кто менял.
            name = unquote(request.headers.get("X-Actor", "")).strip()[:60]
            return {"uid": 0, "name": name or "Гость", "link": True}
        return None
    uid = data.get("uid")
    if ctx.is_admin and isinstance(uid, int) and uid >= 0 and not ctx.is_admin(uid):
        return None  # админа убрали из списка — сессия больше не действует
    return data


def _actor(admin: dict) -> str:
    if admin.get("link"):
        return f"{admin.get('name', 'Гость')} (по ссылке)"
    return f"{admin.get('name', '')} ({admin.get('uid')})"


@web.middleware
async def view_guard(request: web.Request, handler):
    ctx = request.app[CTX]
    token = ctx.cfg.view_token
    open_paths = ("/health", "/auth/link", "/auth/demo", "/favicon.ico")
    if not token or request.path.startswith(open_paths):
        return await handler(request)
    expected = view_cookie_value(ctx.cfg.session_secret, token)
    if request.cookies.get(VIEW_COOKIE) == expected or _session(request):
        return await handler(request)
    if request.query.get("k") == token:
        resp = _redirect(request.path)
        resp.set_cookie(VIEW_COOKIE, expected, max_age=90 * 24 * 3600, httponly=True, samesite="Lax",
                        secure=request.secure)
        return resp
    if request.path.startswith("/api/"):
        raise web.HTTPForbidden(text="Нужна ссылка на дашборд")
    return _page("<h2 style='margin-top:0'>Focus Pulse</h2><p>Откройте дашборд по полной ссылке, которую прислал "
                 "администратор (с <code>?k=…</code> в конце).</p><hr style='border:0;border-top:1px solid #e6e9e3;margin:20px 0'>"
                 + _bot_login_button(ctx), 403)


@web.middleware
async def csrf_guard(request: web.Request, handler):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if request.headers.get("X-Requested-With") != "fetch":
            raise web.HTTPForbidden(text="Запрос отклонён")
    return await handler(request)


def require_admin(request: web.Request) -> dict:
    admin = _admin(request)
    if not admin:
        raise web.HTTPUnauthorized(text="Войдите как администратор")
    return admin


def _json_error(errors: list[str], status: int = 422) -> web.Response:
    return web.json_response({"ok": False, "errors": errors}, status=status)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Ожидался JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Ожидался объект JSON")
    return data


# ---------------------------------------------------------------- маршруты
async def index(request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace('/static/app.css"', f'/static/app.css?v={BUILD}"').replace('/static/app.js"', f'/static/app.js?v={BUILD}"')
    html = html.replace("<head>", f'<head>\n<meta name="fp-build" content="{BUILD}">', 1)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response({"ok": True, "health": ctx.health.status, "pending_writes": ctx.repo.pending_changes})


async def api_state(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response(state_payload(ctx, _admin(request)))


async def api_issue(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    iid = request.match_info["issue_id"]
    rec = ctx.repo.get("Issues", iid)
    if not rec:
        raise web.HTTPNotFound(text="Обращение не найдено")
    return web.json_response(issue_detail(ctx, rec))


def issue_detail(ctx: AppContext, rec: dict) -> dict:
    repo = ctx.repo
    titles = _chat_titles(repo)
    by_issue = _messages_by_issue(repo)
    msgs = by_issue.get(rec["issue_id"], [])
    view = issue_view(repo, rec, msgs, titles, _participants(repo), active_links(repo))
    hist = [a for a in repo.rows("Audit") if a.get("object_id") == rec["issue_id"]][::-1]
    base = (parse_dt(rec.get("accepted_at", "")) or now()).date()
    return {
        "issue": view,
        "messages": [message_view(m, titles) for m in msgs],
        "audit": hist[:100],
        "suggested_due": default_due(repo, rec.get("priority", ""), base, rec.get("platform", "")),
        "merge_targets": [
            {"issue_id": r["issue_id"], "title": r.get("title", "")} for r in repo.rows("Issues")
            if r["issue_id"] != rec["issue_id"] and not r.get("merged_into")
            and r.get("status") not in {"Устранено", "Закрыто", "Реализовано", "Отвечено", "Отклонено"}
        ],
    }


async def api_issue_save(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    try:
        rec = validate_and_save(ctx.repo, request.match_info["issue_id"], data.get("fields") or {}, _actor(admin),
                                accept=bool(data.get("accept")))
    except ValidationError as exc:
        return _json_error(exc.errors)
    await ctx.repo.commit()
    return web.json_response({"ok": True, **issue_detail(ctx, rec)})


async def api_issue_merge(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    try:
        rec = merge_issues(ctx.repo, request.match_info["issue_id"], str(data.get("target", "")), _actor(admin))
    except ValidationError as exc:
        return _json_error(exc.errors)
    await ctx.repo.commit()
    return web.json_response({"ok": True, **issue_detail(ctx, rec)})


async def api_issue_unlink(request: web.Request) -> web.Response:
    """Снять ошибочную связь или разъединить объединённые обращения."""
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    iid = request.match_info["issue_id"]
    try:
        unlink_issues(ctx.repo, iid, str(data.get("other", "")), str(data.get("relation", "")), _actor(admin))
    except ValidationError as exc:
        return _json_error(exc.errors)
    await ctx.repo.commit()
    return web.json_response({"ok": True, **issue_detail(ctx, ctx.repo.get("Issues", iid))})


async def api_jira_get(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    rec = ctx.repo.get("Issues", request.match_info["issue_id"])
    if not rec:
        raise web.HTTPNotFound(text="Обращение не найдено")
    generated = build_jira_text(ctx.repo, rec)
    return web.json_response({
        "text": rec.get("jira_text") or generated, "generated": generated, "saved": bool(rec.get("jira_text")),
        "stale": bool(rec.get("jira_text")) and rec.get("jira_text") != generated,
        "jira_key": rec.get("jira_key", ""), "type": rec.get("type", ""),
    })


async def api_jira_save(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    iid = request.match_info["issue_id"]
    rec = ctx.repo.get("Issues", iid)
    if not rec:
        raise web.HTTPNotFound(text="Обращение не найдено")
    data = await _body(request)
    changes = {}
    if "jira_text" in data:
        changes["jira_text"] = "" if data.get("regenerate") else str(data["jira_text"])[:20000]
    if "jira_key" in data:
        changes["jira_key"] = str(data["jira_key"]).strip().upper()[:40]
    if changes:
        changes["updated_at"] = iso()
        old = ctx.repo.update("Issues", iid, changes)
        audit_changes(ctx.repo, _actor(admin), iid, old, ctx.repo.get("Issues", iid))
        await ctx.repo.commit()
    return await api_jira_get(request)


async def api_message_action(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    try:
        if data.get("action") == "split":
            result = split_message(ctx.repo, request.match_info["msg_id"], str(data.get("issue_id", "")), _actor(admin))
        else:
            result = message_action(ctx.repo, request.match_info["msg_id"], str(data.get("action", "")), _actor(admin),
                                    str(data.get("reason", "")), str(data.get("issue_id", "")))
    except ValidationError as exc:
        return _json_error(exc.errors)
    await ctx.repo.commit()
    return web.json_response({"ok": True, "issue_id": result.get("issue_id", "") if "type" in result else ""})


async def api_chat(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    cid = request.match_info["chat_id"]
    if not ctx.repo.get("Chats", cid):
        raise web.HTTPNotFound(text="Чат не найден")
    data = await _body(request)
    changes = {}
    if "active" in data:
        changes["active"] = "1" if data["active"] in (True, "1", 1) else "0"
    if "default_platform" in data:
        if data["default_platform"] not in ("", "iOS", "Android"):
            return _json_error(["Платформа чата: iOS, Android или пусто"])
        changes["default_platform"] = data["default_platform"]
    if "title" in data and str(data["title"]).strip():
        changes["title"] = str(data["title"]).strip()[:100]
    old = ctx.repo.update("Chats", cid, changes)
    audit_changes(ctx.repo, _actor(admin), cid, old, ctx.repo.get("Chats", cid))
    await ctx.repo.commit()
    return web.json_response({"ok": True})


async def api_operations(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    repo = ctx.repo
    data = await _body(request)
    action = data.get("action")
    section = str(data.get("section", "")).strip()
    operation = str(data.get("operation", "")).strip()
    actor = _actor(admin)
    key = f"{section} / {operation}"
    if section not in SECTIONS:
        return _json_error(["Выберите раздел"])
    if not operation:
        return _json_error(["Укажите операцию"])
    rec = repo.get("Operations", section, operation)
    if action == "add":
        if rec:
            return _json_error(["Такая операция уже есть"])
        repo.insert("Operations", {"section": section, "operation": operation[:80],
                                   "keywords": str(data.get("keywords", ""))[:500], "status": "Утверждена",
                                   "created_at": iso()})
        audit(repo, actor, key, "Операция", "", "добавлена")
    elif not rec:
        return _json_error(["Операция не найдена"])
    elif action == "approve":
        audit_changes(repo, actor, key, repo.update("Operations", rec_key(section, operation), {"status": "Утверждена"}), rec)
    elif action == "reject":
        audit_changes(repo, actor, key, repo.update("Operations", rec_key(section, operation), {"status": "Отклонена"}), rec)
    elif action == "keywords":
        audit_changes(repo, actor, key, repo.update("Operations", rec_key(section, operation),
                                                    {"keywords": str(data.get("keywords", ""))[:500]}), rec)
    elif action == "merge":
        target = str(data.get("target", "")).strip()
        if not repo.get("Operations", section, target) or target == operation:
            return _json_error(["Выберите операцию того же раздела для объединения"])
        audit_changes(repo, actor, key, repo.update("Operations", rec_key(section, operation),
                                                    {"merged_into": target, "status": "Объединена"}), rec)
        moved = 0
        for issue in repo.rows("Issues"):
            if issue.get("section") == section and issue.get("operation") == operation:
                old = repo.update("Issues", issue["issue_id"], {"operation": target, "updated_at": iso()})
                audit_changes(repo, actor, issue["issue_id"], old, issue)
                moved += 1
    else:
        return _json_error(["Неизвестное действие"])
    await repo.commit()
    return web.json_response({"ok": True})


def rec_key(*parts: str) -> str:
    return Repo.make_key(*parts)


def _release_fields(data: dict) -> tuple[str, str, str, list[str]]:
    version = str(data.get("version", "")).strip()
    platform = str(data.get("platform", "")).strip()
    released = str(data.get("released_at", "")).strip()[:10]
    errors = []
    if not parse_version(version):
        errors.append("Укажите версию в формате 4.2.1")
    if platform not in ("iOS", "Android", "iOS и Android"):
        errors.append("Выберите платформу")
    if not parse_date(released):
        errors.append("Укажите дату выхода")
    return version, platform, released, errors


async def api_release_edit(request: web.Request) -> web.Response:
    """Правка релиза. Версия и платформа — ключ строки, поэтому при их смене строка заменяется."""
    admin = require_admin(request)
    ctx, repo = request.app[CTX], request.app[CTX].repo
    data = await _body(request)
    old_v, old_p = str(data.get("old_version", "")).strip(), str(data.get("old_platform", "")).strip()
    old = repo.get("Releases", old_v, old_p)
    if not old:
        return _json_error(["Релиз не найден — обновите страницу"])
    version, platform, released, errors = _release_fields(data)
    if errors:
        return _json_error(errors)
    actor = _actor(admin)
    if (version, platform) == (old_v, old_p):
        if released != old.get("released_at"):
            repo.update("Releases", rec_key(version, platform), {"released_at": released})
            audit(repo, actor, f"Релиз {version}", "Дата выхода", old.get("released_at", ""), released)
            await repo.commit()
        return web.json_response({"ok": True})
    if repo.get("Releases", version, platform):
        return _json_error([f"Релиз {version} · {platform} уже заведён"])
    created_by = old.get("created_by", "")
    await repo.delete_where({"Releases": lambda r: r.get("version", "").strip() == old_v and r.get("platform", "").strip() == old_p})
    repo.insert("Releases", {"version": version, "platform": platform, "released_at": released, "created_by": created_by})
    audit(repo, actor, f"Релиз {version}", "Релиз изменён", f"{old_v} · {old_p} · {old.get('released_at', '')}",
          f"{version} · {platform} · {released}")
    await repo.commit()
    return web.json_response({"ok": True})


async def api_release_delete(request: web.Request) -> web.Response:
    admin = require_admin(request)
    repo = request.app[CTX].repo
    data = await _body(request)
    v, p = str(data.get("version", "")).strip(), str(data.get("platform", "")).strip()
    old = repo.get("Releases", v, p)
    if not old:
        return _json_error(["Релиз не найден — обновите страницу"])
    await repo.delete_where({"Releases": lambda r: r.get("version", "").strip() == v and r.get("platform", "").strip() == p})
    audit(repo, _actor(admin), f"Релиз {v}", "Релиз удалён", f"{p} · {old.get('released_at', '')}", "")
    await repo.commit()
    return web.json_response({"ok": True})


async def api_release(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    version, platform, released, errors = _release_fields(data)
    if errors:
        return _json_error(errors)
    if ctx.repo.get("Releases", version, platform):
        return _json_error(["Такой релиз уже заведён"])
    ctx.repo.insert("Releases", {"version": version, "platform": platform, "released_at": released,
                                 "created_by": _actor(admin)})
    audit(ctx.repo, _actor(admin), f"Релиз {version}", "Релиз", "", f"{platform}, {released}")
    await ctx.repo.commit()
    sent = 0
    if data.get("notify") and ctx.release_notifier:
        sent = await ctx.release_notifier(version, platform)
    return web.json_response({"ok": True, "notified": sent})


async def api_settings(request: web.Request) -> web.Response:
    admin = require_admin(request)
    ctx = request.app[CTX]
    data = await _body(request)
    allowed = {"auto_analysis", "filter_greetings", "filter_organization", "filter_reactions"}
    for key, value in data.items():
        if key not in allowed:
            continue
        value = "1" if value in (True, "1", 1) else "0"
        old = ctx.repo.set_setting(key, value)
        if old:
            audit(ctx.repo, _actor(admin), "Настройки", key, old.get("value", ""), value)
    await ctx.repo.commit()
    return web.json_response({"ok": True})


async def api_run(request: web.Request) -> web.Response:
    require_admin(request)
    ctx = request.app[CTX]
    if ctx.analyzer.running:
        return web.json_response({"ok": True, "started": False, "running": True})

    async def _go():
        try:
            await ctx.repo.commit()
        except Exception:
            log.warning("pre-run commit failed", exc_info=True)
        await ctx.analyzer.run("dashboard", force=True)

    ctx.run_task = asyncio.create_task(_go())
    await asyncio.sleep(0)
    return web.json_response({"ok": True, "started": True})


async def api_reload(request: web.Request) -> web.Response:
    require_admin(request)
    ctx = request.app[CTX]
    await ctx.repo.reload()
    return web.json_response({"ok": True})


async def api_image(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    msg_id, idx = request.match_info["msg_id"], to_int(request.match_info["idx"], -1)
    msg = ctx.repo.get("Messages", msg_id)
    screens = jloads((msg or {}).get("screens", ""), [])
    if not msg or idx < 0 or idx >= len(screens) or screens[idx].get("status") != "ok":
        raise web.HTTPNotFound(text="Скрин недоступен")
    entry = screens[idx]
    if entry.get("demo"):
        path = DEMO_SCREENS / f"{entry['demo']}.svg"
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/svg+xml", "Cache-Control": "private, max-age=3600"})
    file_id = entry.get("file_id", "")
    data = ctx.image_cache.get(file_id)
    if data is None:
        if not ctx.image_fetcher:
            raise web.HTTPNotFound(text="Бот не подключён")
        try:
            data = await ctx.image_fetcher(file_id)
        except Exception:
            log.warning("image fetch failed", exc_info=True)
            raise web.HTTPBadGateway(text="Не удалось загрузить скрин из Telegram")
        ctx.image_cache[file_id] = data
        while len(ctx.image_cache) > 120:
            ctx.image_cache.popitem(last=False)
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- вход
def _redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


def _set_session(resp: web.StreamResponse, ctx: AppContext, uid: int, name: str, secure: bool) -> None:
    resp.set_cookie(SESSION_COOKIE, sign_session(ctx.cfg.session_secret, uid, name), max_age=7 * 24 * 3600,
                    httponly=True, samesite="Lax", secure=secure)


def _page(body: str, status: int = 200) -> web.Response:
    return web.Response(status=status, content_type="text/html", headers={"Cache-Control": "no-store"}, text=(
        "<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Focus Pulse</title><body style='font:16px/1.6 Inter,Arial,sans-serif;background:#f5f6f2;color:#202924;"
        "margin:0;padding:40px 16px'><div style='max-width:520px;margin:auto;background:#fff;border:1px solid #e6e9e3;"
        "border-radius:12px;padding:28px'>" + body + "</div></body></html>"))


_BTN = ("display:inline-block;background:#286546;color:#fff;border:0;border-radius:8px;padding:12px 18px;"
        "font:inherit;cursor:pointer;text-decoration:none")


def _bot_login_button(ctx: AppContext) -> str:
    if not ctx.bot_username:
        return "<p>Администратор: отправьте боту команду <b>/login</b> и откройте ссылку из ответа.</p>"
    return (f"<p><a style='{_BTN}' href='https://t.me/{html_escape(ctx.bot_username)}?start=login'>"
            "Я администратор — войти через Telegram</a></p><p style='color:#78817b;font-size:13px'>"
            "Откроется бот и пришлёт ссылку для входа. Можно и так: отправьте боту /login.</p>")


async def auth_link(request: web.Request) -> web.Response:
    """GET показывает кнопку «Войти», вход — по POST. Так ссылку не «съедают» предпросмотр Telegram,
    предзагрузка браузера и проверка ссылок антивирусом."""
    ctx = request.app[CTX]
    token = request.query.get("t", "")
    if request.method == "POST":
        form = await request.post()
        token = str(form.get("t", ""))
    item = (ctx.tokens.consume(token) if request.method == "POST" else ctx.tokens.peek(token)) if ctx.tokens else None
    if not item:
        return _page("<h2 style='margin-top:0'>Ссылка для входа недействительна или устарела</h2>"
                     "<p>Получите новую — она действует 15 минут и только один раз.</p>" + _bot_login_button(ctx), 403)
    uid, name = item
    if request.method != "POST":
        return _page(f"<h2 style='margin-top:0'>Вход на дашборд Focus Pulse</h2><p>Вы входите как <b>{html_escape(name)}</b>.</p>"
                     f"<form method=post action='/auth/link'><input type=hidden name=t value='{html_escape(token)}'>"
                     f"<button style='{_BTN}' type=submit>Войти</button></form>"
                     "<p style='color:#78817b;font-size:13px'>Вход сохранится в этом браузере на 7 дней. "
                     "Если страница открылась внутри Telegram, сначала откройте её в обычном браузере (⋯ → «Открыть в браузере»).</p>")
    resp = _redirect("/")
    _set_session(resp, ctx, uid, name, request.secure)
    if ctx.cfg.view_token:
        resp.set_cookie(VIEW_COOKIE, view_cookie_value(ctx.cfg.session_secret, ctx.cfg.view_token),
                        max_age=90 * 24 * 3600, httponly=True, samesite="Lax", secure=request.secure)
    audit(ctx.repo, f"{name} ({uid})", "Дашборд", "Вход", "", "по ссылке из бота")
    return resp


async def auth_telegram(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    data = await _body(request)
    if not verify_telegram_login(ctx.cfg.telegram_token, data):
        return _json_error(["Подпись Telegram не прошла проверку"], 403)
    uid = to_int(data.get("id"), -1)
    if not ctx.is_admin or not ctx.is_admin(uid):
        return _json_error(["Этот аккаунт Telegram не входит в список администраторов"], 403)
    name = " ".join(x for x in [data.get("first_name", ""), data.get("last_name", "")] if x) or str(uid)
    resp = web.json_response({"ok": True})
    _set_session(resp, ctx, uid, name, request.secure)
    audit(ctx.repo, f"{name} ({uid})", "Дашборд", "Вход", "", "Telegram Login")
    return resp


async def auth_demo(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    if not ctx.cfg.demo_mode:
        raise web.HTTPNotFound()
    resp = _redirect("/")
    _set_session(resp, ctx, -1, "Демо-администратор", request.secure)
    return resp


async def auth_logout(request: web.Request) -> web.Response:
    resp = web.json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE)
    return resp


@web.middleware
async def no_stale_static(request: web.Request, handler):
    """Код дашборда браузер перепроверяет при каждом открытии — иначе после выкладки остаётся старая версия."""
    resp = await handler(request)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


STALE_MSG = ("Открыта устаревшая версия дашборда — действия в ней не сохранятся. "
             "Перезагрузите страницу: Ctrl+Shift+R (на Mac — Cmd+Shift+R).")


@web.middleware
async def stale_client(request: web.Request, handler):
    """Страница со старым кодом (открыта до выкладки) не работает молча по-старому, а просит перезагрузиться."""
    ctx = request.app[CTX]
    if (not ctx.cfg.demo_mode and request.path.startswith("/api/") and not request.path.startswith(("/api/auth", "/api/image"))
            and request.headers.get("X-FP-Build") != BUILD):
        return web.json_response({"errors": [STALE_MSG], "reload": True}, status=409)
    return await handler(request)


def create_app(ctx: AppContext) -> web.Application:
    app = web.Application(middlewares=[no_stale_static, view_guard, csrf_guard, stale_client],
                          client_max_size=2 * 1024 * 1024)
    app[CTX] = ctx
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/issues/{issue_id}", api_issue)
    app.router.add_post("/api/issues/{issue_id}/save", api_issue_save)
    app.router.add_post("/api/issues/{issue_id}/merge", api_issue_merge)
    app.router.add_post("/api/issues/{issue_id}/unlink", api_issue_unlink)
    app.router.add_get("/api/issues/{issue_id}/jira", api_jira_get)
    app.router.add_post("/api/issues/{issue_id}/jira", api_jira_save)
    app.router.add_post("/api/messages/{msg_id}/action", api_message_action)
    app.router.add_post("/api/chats/{chat_id}", api_chat)
    app.router.add_post("/api/operations", api_operations)
    app.router.add_post("/api/releases", api_release)
    app.router.add_post("/api/releases/edit", api_release_edit)
    app.router.add_post("/api/releases/delete", api_release_delete)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/run", api_run)
    app.router.add_post("/api/reload", api_reload)
    app.router.add_get("/api/image/{msg_id}/{idx}", api_image)
    app.router.add_get("/auth/link", auth_link)
    app.router.add_post("/auth/link", auth_link)
    app.router.add_get("/auth/demo", auth_demo)
    app.router.add_post("/api/auth/telegram", auth_telegram)
    app.router.add_post("/api/logout", auth_logout)
    app.router.add_static("/static/", STATIC, show_index=False)
    return app


__all__ = ["AppContext", "Health", "create_app", "FIELD_NAMES"]
