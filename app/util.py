"""Время, версии, рабочие дни и мелкие помощники."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Tashkent")


def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(TZ).isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value[:10] + "T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def today() -> date:
    return now().date()


def parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def add_working_days(start: date, days: int) -> date:
    """Пн–Пт; праздники Узбекистана не учитываются."""
    d = start
    left = days
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")


def parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    m = _VERSION_RE.search(str(value))
    if not m:
        return None
    parts = [int(p) for p in m.group(0).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def compare_versions(a: str | None, b: str | None) -> int | None:
    """-1 / 0 / 1; None, если одну из версий не разобрать. 4.10.0 новее 4.9.2."""
    va, vb = parse_version(a), parse_version(b)
    if va is None or vb is None:
        return None
    return (va > vb) - (va < vb)


def jloads(value: str, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def jdumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def split_list(value: str) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def join_list(values) -> str:
    seen: list[str] = []
    for v in values:
        v = str(v).strip()
        if v and v not in seen:
            seen.append(v)
    return ",".join(seen)


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def chat_link(chat_id: str | int, message_id: str | int, thread_id: str | int | None = None) -> str:
    cid = str(chat_id)
    if cid.startswith("-100"):
        internal = cid[4:]
    else:
        internal = cid.lstrip("-")
    if thread_id:
        return f"https://t.me/c/{internal}/{thread_id}/{message_id}"
    return f"https://t.me/c/{internal}/{message_id}"


def short(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
