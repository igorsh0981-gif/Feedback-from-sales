"""Настройки из переменных окружения (Railway → Variables)."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


@dataclass
class Config:
    telegram_token: str = ""
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"
    sheet_id: str = ""
    google_service_account_json: str = ""
    admin_ids: set[int] = field(default_factory=set)
    service_channel_id: int | None = None
    dashboard_url: str = ""
    analysis_interval_min: int = 20
    episode_pause_min: int = 10
    claude_daily_limit: int = 2000
    port: int = 8080
    session_secret: str = ""
    view_token: str = ""
    demo_mode: bool = False
    tz: str = "Asia/Tashkent"
    tesseract_langs: str = "rus+uzb+uzb_cyrl+eng"
    low_confidence: float = 0.6

    @property
    def bot_enabled(self) -> bool:
        return bool(self.telegram_token)

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


def load_config() -> Config:
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    channel = os.getenv("SERVICE_CHANNEL_ID", "").strip()
    secret = os.getenv("SESSION_SECRET", "").strip()
    if not secret:
        # Стабильный секрет между перезапусками, если отдельный не задан.
        secret = hashlib.sha256(("focus-pulse:" + (token or "demo")).encode()).hexdigest()
    return Config(
        telegram_token=token,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-sonnet-5-5",
        sheet_id=os.getenv("SHEET_ID", "").strip(),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        admin_ids=_ids(os.getenv("ADMIN_IDS", "")),
        service_channel_id=int(channel) if channel.lstrip("-").isdigit() else None,
        dashboard_url=os.getenv("DASHBOARD_URL", "").strip().rstrip("/"),
        analysis_interval_min=max(1, _int("ANALYSIS_INTERVAL_MIN", 20)),
        episode_pause_min=max(0, _int("EPISODE_PAUSE_MIN", 10)),
        claude_daily_limit=max(1, _int("CLAUDE_DAILY_LIMIT", 2000)),
        port=_int("PORT", 8080),
        session_secret=secret,
        view_token=os.getenv("VIEW_TOKEN", "").strip(),
        demo_mode=os.getenv("DEMO_MODE", "").strip().lower() in {"1", "true", "yes"},
        tesseract_langs=os.getenv("TESSERACT_LANGS", "").strip() or "rus+uzb+uzb_cyrl+eng",
    )
