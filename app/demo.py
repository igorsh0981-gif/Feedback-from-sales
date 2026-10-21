"""Демо-режим (DEMO_MODE=1): данные из выгрузки прототипа, без Telegram, Sheets и Claude.

Даты сдвигаются так, чтобы последняя приходилась на сегодня. Расхождения выгрузки
с правилами ТЗ (раздел 16) исправлены: статусы по типу, сроки, версии у сообщений, регрессии.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from .constants import DEFAULT_OPERATIONS, REL_REGRESSION
from .services import recount_issue
from .storage.repo import Repo
from .util import add_working_days, iso, jdumps, now

DEMO_XML = Path(__file__).resolve().parents[1] / "demo" / "focus_pulse_demo.xml"
BASE_DATE = date(2026, 10, 21)

SECTION_MAP = {"Вход": "Вход и регистрация", "Регистрация": "Вход и регистрация", "Настройки": "Профиль и настройки",
               "Не определён": "Прочее"}
OPERATION_MAP = {
    "FP-142": "Перевод на карту", "FP-128": "Перевод на карту", "FP-143": "Чек об оплате",
    "FP-144": "История переводов", "FP-145": "Просмотр карты", "FP-146": "Перевод на карту",
    "FP-147": "Главный экран", "FP-148": "Вход в приложение", "FP-149": "Push-уведомления",
    "FP-150": "Перевод на карту", "FP-151": "Оплата мобильной связи", "FP-201": "Оформление вклада",
    "FP-202": "Пополнение вклада", "FP-203": "Доходность и начисления", "FP-204": "Оформление вклада",
    "FP-205": "Доходность и начисления", "FP-206": "Оплата мобильной связи", "FP-207": "Обмен валюты",
    "FP-208": "Регистрация",
}
TYPE_FIX = {"FP-146": "Вопрос"}
STATUS_MAP = {
    "Баг": {"Требует воспроизведения": "Принято", "Подтверждено": "Принято", "Исправлено": "Устранено"},
    "Пожелание": {"Требует воспроизведения": "Оценка", "Подтверждено": "Оценка"},
    "UX": {"Требует воспроизведения": "Оценка", "Подтверждено": "Оценка"},
    "Вопрос": {"Уточнение": "Новое"},
}
OWNER_FIX = {"FP-201": "iOS"}
SCREENS = {"FP-142": "transfer", "FP-143": "receipt", "FP-148": "login"}
PLATFORM_OF_CHAT = {"DEMO-CHAT-0": "Android", "DEMO-CHAT-1": "iOS", "DEMO-CHAT-2": ""}
DEVICES = {"Android": "Samsung A54", "iOS": "iPhone 13"}


def _txt(el, tag: str) -> str:
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def seed_demo(repo: Repo, xml_path: Path = DEMO_XML) -> None:
    root = ET.parse(xml_path).getroot()
    shift = (now().date() - BASE_DATE).days

    def d(value: str) -> str:
        if not value:
            return ""
        return (date.fromisoformat(value) + timedelta(days=shift)).isoformat()

    def dt(day: str, time: str = "10:32") -> str:
        return f"{d(day)}T{time}:00+05:00"

    chat_ids = {}
    for n, chat in enumerate(root.iter("chat")):
        cid = f"-100900000000{n}"
        chat_ids[chat.get("id")] = cid
        repo.insert("Chats", {"chat_id": cid, "title": chat.text, "default_platform": PLATFORM_OF_CHAT.get(chat.get("id"), ""),
                              "active": "1", "added_at": dt("2026-10-14", "09:00")})
    for sec, op, kw in DEFAULT_OPERATIONS:
        repo.insert("Operations", {"section": sec, "operation": op, "keywords": kw, "status": "Утверждена",
                                   "created_at": dt("2026-10-14", "09:00")})
    repo.insert("Operations", {"section": "Платежи", "operation": "Оплата штрафов", "keywords": "",
                               "status": "Предложена", "created_at": dt("2026-10-20", "11:00")})
    repo.insert("Releases", {"version": "4.1.9", "platform": "iOS и Android", "released_at": d("2026-10-17"),
                             "created_by": "Демо"})
    repo.insert("Releases", {"version": "4.2.0", "platform": "iOS и Android", "released_at": d("2026-10-19"),
                             "created_by": "Демо"})
    repo.insert("Releases", {"version": "4.2.1", "platform": "iOS и Android", "released_at": d("2026-10-28"),
                             "created_by": "Демо"})

    msg_seq = 1000
    senders: dict[str, dict] = {}
    for issue in root.iter("issue"):
        iid = issue.get("id")
        itype = TYPE_FIX.get(iid, _txt(issue, "type"))
        status = STATUS_MAP.get(itype, {}).get(_txt(issue, "status"), _txt(issue, "status"))
        review = _txt(issue, "requiresReview") == "true"
        if review:
            status = "Новое"
        created = d(_txt(issue, "created"))
        priority = _txt(issue, "priority")
        accepted = not review and status != "Новое"
        due = d(_txt(issue, "due"))
        if itype == "Баг" and accepted and status not in ("Устранено", "Закрыто"):
            base = date.fromisoformat(created)
            if not due or due < created:
                due = add_working_days(base, 1 if priority == "Критичный" else 3).isoformat()
        version = _txt(issue, "version")
        section = SECTION_MAP.get(_txt(issue, "area"), _txt(issue, "area"))
        platform = _txt(issue, "platform") or "Не указана"
        repo.insert("Issues", {
            "issue_id": iid, "created_at": dt(_txt(issue, "created")), "updated_at": dt(_txt(issue, "created"), "10:40"),
            "type_ai": _txt(issue, "type"), "type": itype, "section": section, "operation": OPERATION_MAP.get(iid, ""),
            "platform": platform, "device": DEVICES.get(platform, ""), "app_version": version,
            "priority_ai": priority, "priority": priority, "status": status, "title": _txt(issue, "title"),
            "summary": _txt(issue, "text"), "reason_ai": _txt(issue, "reason"),
            "actual": _txt(issue, "text") if itype == "Баг" else "", "confidence": "0.62" if review else "0.86",
            "owner": OWNER_FIX.get(iid, {"Продукт": "BA", "Дизайн": "BA"}.get(_txt(issue, "owner"), _txt(issue, "owner"))), "accepted_by": "Демо-администратор" if accepted else "",
            "accepted_at": dt(_txt(issue, "created"), "11:00") if accepted else "",
            "due": due if itype == "Баг" else "", "due_set_by": "Демо-администратор" if due and accepted else "",
            "resolved_at": d(_txt(issue, "resolvedAt")), "fix_version": _txt(issue, "fixVersion"),
            "resolution": _txt(issue, "resolution"),
        })
        for n, rep in enumerate(issue.iter("reporter")):
            sender = rep.find("sender")
            sid = sender.get("id")
            name = _txt(sender, "displayName")
            uname = sender.find("username")
            chat_ref = rep.get("chatRef")
            plat = PLATFORM_OF_CHAT.get(chat_ref) or (platform if platform in ("iOS", "Android") else "")
            senders.setdefault(sid, {"telegram_id": sid, "name": name,
                                     "username": (uname.text or "") if uname is not None and uname.get("available") == "true" else "",
                                     "platform": plat, "device": DEVICES.get(plat, ""), "app_version": "4.2.0",
                                     "updated_at": dt("2026-10-14", "09:00")})
            msg_seq += 1
            cid = chat_ids[chat_ref]
            text = _txt(rep, "text")
            if "Демонстрационный повторный сигнал" in text:
                text = ["У меня то же самое.", "Подтверждаю, повторяется.", "Тоже столкнулся.", "+1, на моём телефоне так же.",
                        "И у меня, проверил дважды."][n % 5]
            screens = ""
            if n == 0 and iid in SCREENS:
                screens = jdumps([{"status": "ok", "demo": SCREENS[iid], "ocr": "", "masked": ""}])
            repo.insert("Messages", {
                "msg_id": f"{cid}:{msg_seq}", "chat_id": cid, "chat_title": "", "message_id": str(msg_seq),
                "author_id": sid, "author_name": name, "sent_at": dt(_txt(rep, "date"), _txt(rep, "time")),
                "kind": "photo" if screens else "text", "platform": plat, "device": DEVICES.get(plat, ""),
                "app_version": version, "text_masked": text, "screens": screens,
                "screen_status": "ok" if screens else "", "link": f"https://t.me/c/{cid[4:]}/{msg_seq}",
                "episode_id": f"E-{cid}:{msg_seq}", "state": "Учтено", "state_reason": _txt(issue, "reason"),
                "state_by": "AI", "issue_id": iid, "updated_at": iso(),
            })
    for sender in senders.values():
        repo.insert("Participants", sender)

    for msg in root.iter("message"):
        msg_seq += 1
        cid = chat_ids[msg.get("chatRef")]
        sender = msg.find("sender")
        state = _txt(msg, "classification")
        issue_ref = msg.find("issueRef")
        iid = issue_ref.get("id") if issue_ref is not None else ""
        if state == "Учтено" and iid == "FP-142":
            state_reason = "Повторный сигнал к FP-142; связан с ранее закрытым FP-128."
        else:
            state_reason = _txt(msg, "reason")
        repo.insert("Messages", {
            "msg_id": f"{cid}:{msg_seq}", "chat_id": cid, "message_id": str(msg_seq),
            "author_id": sender.get("id"), "author_name": _txt(sender, "displayName"),
            "sent_at": dt("2026-10-21", f"{11 + (msg_seq % 3):02d}:{(msg_seq * 7) % 60:02d}"),
            "kind": "text", "text_masked": _txt(msg, "text"), "state": state, "state_reason": state_reason,
            "state_by": "Правила" if state == "Отфильтровано" else "AI", "issue_id": iid,
            "link": f"https://t.me/c/{cid[4:]}/{msg_seq}", "app_version": "4.2.0",
            "platform": PLATFORM_OF_CHAT.get(msg.get("chatRef"), ""), "updated_at": iso(),
        })

    # По правилу версий (раздел 8): повтор на 4.2.0 после исправления в 4.1.9 — регрессия.
    for new_id, old_id in (("FP-142", "FP-128"), ("FP-204", "FP-201")):
        repo.insert("Links", {"issue_id": new_id, "related_id": old_id, "relation": REL_REGRESSION,
                              "created_by": "AI", "created_at": dt("2026-10-21", "10:40")})
    repo.insert("Issues", {
        "issue_id": "FP-209", "created_at": dt("2026-10-21", "09:50"), "updated_at": dt("2026-10-21", "10:40"),
        "type_ai": "Баг", "type": "Баг", "section": "Платежи", "operation": "Оплата штрафов", "platform": "Android",
        "device": "Samsung A54", "app_version": "4.2.0", "priority_ai": "Средний", "priority": "Средний",
        "status": "Новое", "title": "Не находится раздел оплаты штрафов",
        "summary": "Участник не нашёл оплату штрафов ГАИ в платежах.", "reason_ai":
        "Операции нет в справочнике — предложена новая «Оплата штрафов». Возможно, это UX-проблема поиска.",
        "confidence": "0.48", "owner": "Не назначен",
    })
    msg_seq += 1
    cid = chat_ids["DEMO-CHAT-0"]
    repo.insert("Messages", {
        "msg_id": f"{cid}:{msg_seq}", "chat_id": cid, "message_id": str(msg_seq), "author_id": "DEMO-U03",
        "author_name": "Тимур · демо", "sent_at": dt("2026-10-21", "09:50"), "kind": "text",
        "text_masked": "Где оплатить штраф ГАИ? В платежах не нашёл, карта 8600 12•• •••• 4471",
        "mask_summary": "карта ×1", "state": "Учтено", "state_reason": "Новое обращение", "state_by": "AI",
        "issue_id": "FP-209", "link": f"https://t.me/c/{cid[4:]}/{msg_seq}", "app_version": "4.2.0",
        "platform": "Android", "device": "Samsung A54", "updated_at": iso(),
    })
    repo.insert("Admins", {"telegram_id": "-1", "name": "Демо-администратор"})
    for rec in repo.rows("Issues"):
        recount_issue(repo, rec["issue_id"])
        plats = {m.get("platform") for m in repo.rows("Messages") if m.get("issue_id") == rec["issue_id"]}
        if {"iOS", "Android"} <= plats:
            repo.update("Issues", rec["issue_id"], {"platform": "iOS и Android"})
