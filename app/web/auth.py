"""Сессии админа (подписанная cookie), вход по одноразовой ссылке и Telegram Login Widget."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

SESSION_COOKIE = "fp_session"
VIEW_COOKIE = "fp_view"
SESSION_TTL = 7 * 24 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_session(secret: str, uid: int, name: str, ttl: int = SESSION_TTL) -> str:
    payload = _b64(json.dumps({"uid": uid, "name": name, "exp": int(time.time()) + ttl}, ensure_ascii=False).encode())
    sig = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_session(secret: str, value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    payload, sig = value.rsplit(".", 1)
    expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data


def view_cookie_value(secret: str, view_token: str) -> str:
    return _b64(hmac.new(secret.encode(), ("view:" + view_token).encode(), hashlib.sha256).digest())


def verify_telegram_login(bot_token: str, data: dict, max_age: int = 86400) -> bool:
    """Проверка подписи Telegram Login Widget: https://core.telegram.org/widgets/login#checking-authorization"""
    received = str(data.get("hash", ""))
    if not received or not bot_token:
        return False
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False
    try:
        return time.time() - int(fields.get("auth_date", 0)) <= max_age
    except (TypeError, ValueError):
        return False
