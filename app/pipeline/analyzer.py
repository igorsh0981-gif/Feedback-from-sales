"""Запуск анализа: предфильтр → эпизоды → Claude → маршрутизация → запись (раздел 7 ТЗ)."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

from ..constants import (
    LINKED_STATES,
    MSG_FILTERED,
    MSG_QUEUED,
    MSG_REVIEW,
    TERMINAL_STATUSES,
)
from ..services import audit, recount_issue
from ..storage.repo import Repo
from ..util import iso, jloads, now, parse_dt
from .classifier import EpisodeInput, EpisodeMessage
from .episodes import Episode, build_episodes
from .prefilter import detect_noise
from .router import apply_analysis, learn_profile

log = logging.getLogger(__name__)
_PREV_ISSUE_RE = re.compile(r"было в (FP-\d+)")

ImageLoader = Callable[[str], Awaitable[bytes]]
Notifier = Callable[[str], Awaitable[None]]
MAX_CANDIDATES = 150
CLOSED_WINDOW = timedelta(days=90)


@dataclass
class RunResult:
    run_id: str
    trigger: str
    started_at: str
    finished_at: str = ""
    messages: int = 0
    episodes: int = 0
    new_issues: int = 0
    signals: int = 0
    filtered: int = 0
    review: int = 0
    errors: int = 0
    claude_calls: int = 0
    skipped_limit: int = 0
    waiting: int = 0
    new_ids: list[str] = field(default_factory=list)


class Analyzer:
    def __init__(
        self,
        repo: Repo,
        classifier,
        pause_min: int = 10,
        daily_limit: int = 2000,
        image_loader: ImageLoader | None = None,
        notifier: Notifier | None = None,
        interval_min: int = 20,
    ):
        self.repo = repo
        self.classifier = classifier
        self.pause_min = pause_min
        self.daily_limit = daily_limit
        self.image_loader = image_loader
        self.notifier = notifier
        self.interval_min = interval_min
        self.lock = asyncio.Lock()
        self.last_run: RunResult | None = None
        self.next_run_at: datetime | None = None
        self._calls_day: date | None = None
        self._calls = 0
        self._limit_warned: date | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()

    # ---------------------------------------------------------------- лимит Claude
    def _count_call(self) -> None:
        today = now().date()
        if self._calls_day != today:
            self._calls_day, self._calls = today, 0
        self._calls += 1

    def calls_today(self) -> int:
        return self._calls if self._calls_day == now().date() else 0

    def _limit_reached(self) -> bool:
        return self.calls_today() >= self.daily_limit

    async def _check_limit_alert(self) -> None:
        today = now().date()
        if self.calls_today() >= 0.8 * self.daily_limit and self._limit_warned != today and self.notifier:
            self._limit_warned = today
            await self.notifier(
                f"⚠️ Focus Pulse: использовано {self.calls_today()} из {self.daily_limit} вызовов Claude за сутки (80%)."
            )

    # ---------------------------------------------------------------- подготовка
    def _recover_orphans(self) -> int:
        """Сообщения, привязанные к обращению, которого нет в таблице (сбой между записями) — обратно в очередь."""
        fixed = 0
        for m in self.repo.rows("Messages"):
            if m.get("state") in LINKED_STATES and m.get("issue_id") and not self.repo.get("Issues", m["issue_id"]):
                self.repo.update("Messages", m["msg_id"], {"state": MSG_QUEUED, "issue_id": "", "state_reason": "Восстановлено после сбоя записи"})
                fixed += 1
        return fixed

    def _prefs(self) -> dict[str, bool]:
        return {
            "greetings": self.repo.setting_on("filter_greetings"),
            "organization": self.repo.setting_on("filter_organization"),
            "reactions": self.repo.setting_on("filter_reactions"),
        }

    def _candidates(self) -> list[dict]:
        cutoff = now() - CLOSED_WINDOW
        out = []
        for r in self.repo.rows("Issues"):
            if r.get("merged_into"):
                continue
            if r.get("status") in TERMINAL_STATUSES:
                closed = parse_dt(r.get("resolved_at") or r.get("updated_at", ""))
                if not closed or closed < cutoff:
                    continue
            out.append(r)
        out.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
        return out[:MAX_CANDIDATES]

    def _catalog(self) -> list[tuple[str, str]]:
        return [
            (o["section"], o["operation"])
            for o in self.repo.rows("Operations")
            if not o.get("merged_into") and o.get("status") != "Отклонена"
        ]

    def _context_before(self, ep: Episode, limit: int = 5) -> list[str]:
        first = ep.messages[0].get("sent_at", "")
        ids = {m["msg_id"] for m in ep.messages}
        prior = [
            m for m in self.repo.rows("Messages")
            if m.get("chat_id") == ep.chat_id and m.get("sent_at", "") < first and m["msg_id"] not in ids
            and m.get("text_masked")
        ]
        prior.sort(key=lambda m: m.get("sent_at", ""))
        return [m["text_masked"] for m in prior[-limit:]]

    async def _build_input(self, ep: Episode, catalog, candidates) -> EpisodeInput:
        chat = self.repo.get("Chats", ep.chat_id) or {}
        labels: dict[str, str] = {}
        msgs: list[EpisodeMessage] = []
        images: list[bytes] = []
        for m in ep.messages:
            label = labels.setdefault(m.get("author_id", ""), f"Участник {len(labels) + 1}")
            screens = jloads(m.get("screens", ""), [])
            ocr = [s.get("ocr", "") for s in screens if s.get("status") == "ok"]
            msgs.append(EpisodeMessage(
                author=label, sent_at=m.get("sent_at", ""), text=m.get("text_masked", ""),
                platform=m.get("platform", ""), device=m.get("device", ""), app_version=m.get("app_version", ""),
                screens_ocr=ocr,
            ))
            if self.image_loader:
                for s in screens:
                    if s.get("status") == "ok" and s.get("file_id") and len(images) < 4:
                        try:
                            images.append(await self.image_loader(s["file_id"]))
                        except Exception as exc:  # скрин не загрузился — анализируем по тексту
                            log.warning("image load failed %s: %s", s.get("file_id"), exc)
        return EpisodeInput(
            episode_id=ep.id,
            chat_title=chat.get("title", ""),
            chat_platform=chat.get("default_platform", ""),
            messages=msgs,
            images=images,
            context_before=self._context_before(ep),
            reply_texts=ep.reply_texts,
            reply_issue=ep.reply_issue,
            catalog=catalog,
            candidates=[{
                "issue_id": c["issue_id"], "type": c.get("type", ""), "section": c.get("section", ""),
                "operation": c.get("operation", ""), "status": c.get("status", ""), "title": c.get("title", ""),
                "platform": c.get("platform", ""), "app_version": c.get("app_version", ""),
                "fix_version": c.get("fix_version", ""), "error_text": c.get("error_text", ""),
            } for c in candidates],
        )

    def _reject_emptied(self, issue_ids: set[str]) -> None:
        """Обращение, все сообщения которого автор изменил и они ушли в другое, отклоняется с причиной."""
        for iid in issue_ids:
            rec = self.repo.get("Issues", iid)
            if rec and rec.get("status") == "Новое" and not rec.get("accepted_by") and rec.get("msg_count") == "0":
                self.repo.update("Issues", iid, {"status": "Отклонено", "updated_at": iso(),
                                                 "reject_reason": "Автор изменил сообщение; обращение пусто"})
                audit(self.repo, "AI", iid, "Статус", "Новое", "Отклонено")

    # ---------------------------------------------------------------- запуск
    async def run(self, trigger: str = "schedule", force: bool = False) -> RunResult | None:
        if self.lock.locked():
            return None
        async with self.lock:
            started = now()
            res = RunResult(run_id=started.strftime("R-%Y%m%d-%H%M%S"), trigger=trigger, started_at=iso(started))
            try:
                await self._run(res, force)
            except Exception:
                log.exception("Анализ завершился с ошибкой")
                res.errors += 1
            res.finished_at = iso()
            self.repo.insert("Runs", {
                "run_id": res.run_id, "started_at": res.started_at, "finished_at": res.finished_at,
                "trigger": trigger, "messages": res.messages, "episodes": res.episodes,
                "new_issues": res.new_issues, "signals": res.signals, "filtered": res.filtered,
                "review": res.review, "errors": res.errors, "claude_calls": res.claude_calls,
            })
            try:
                await self.repo.commit()
            except Exception:
                log.exception("Не удалось записать результаты анализа — повтор при следующей фиксации")
                res.errors += 1
            self.last_run = res
            self.next_run_at = now() + timedelta(minutes=self.interval_min)
            if self.notifier and res.new_ids:
                crit = [i for i in res.new_ids if (self.repo.get("Issues", i) or {}).get("priority") == "Критичный"]
                text = f"Focus Pulse: новых обращений {len(res.new_ids)} — на утверждение."
                if crit:
                    text += f" Критичные: {', '.join(crit)}."
                try:
                    await self.notifier(text)
                except Exception:
                    log.warning("notify failed", exc_info=True)
            return res

    async def _run(self, res: RunResult, force: bool) -> None:
        self._recover_orphans()
        prefs = self._prefs()
        pending: list[dict] = []
        for m in self.repo.rows("Messages"):
            if m.get("state") != MSG_QUEUED:
                continue
            res.messages += 1
            screens = jloads(m.get("screens", ""), [])
            raw = jloads(m.get("raw_files", ""), [])
            has_media = bool(screens or raw)
            if m.get("kind") in ("voice", "video", "video_note", "audio") and not m.get("text_masked"):
                self.repo.update("Messages", m["msg_id"], {
                    "state": MSG_REVIEW, "state_by": "AI", "updated_at": iso(),
                    "state_reason": "Голосовое или видео не анализируется автоматически — откройте в чате",
                })
                res.review += 1
                continue
            noise = detect_noise(m.get("text_masked", ""), m.get("kind", "text"), has_media, prefs)
            if noise:
                self.repo.update("Messages", m["msg_id"], {
                    "state": MSG_FILTERED, "state_reason": noise.reason, "state_by": "Правила", "updated_at": iso(),
                })
                res.filtered += 1
                continue
            pending.append(m)

        all_by_id = self.repo.tables["Messages"]
        ready, waiting = build_episodes(pending, all_by_id, self.pause_min, now(), force)
        res.waiting = len(waiting)
        catalog = self._catalog()
        touched: set[str] = set()
        edited_from: set[str] = set()
        for n, ep in enumerate(ready, 1):
            if self._limit_reached():
                res.skipped_limit = len(ready) - n + 1
                log.warning("Достигнут суточный лимит вызовов Claude; %s эпизодов ждут", res.skipped_limit)
                break
            candidates = self._candidates()
            try:
                inp = await self._build_input(ep, catalog, candidates)
                self._count_call()
                res.claude_calls += 1
                analysis = await self.classifier.classify(inp)
            except Exception:
                log.exception("Не удалось разобрать эпизод %s — останется в очереди", ep.id)
                res.errors += 1
                continue
            for m in ep.messages:
                edited_from.update(_PREV_ISSUE_RE.findall(m.get("state_reason", "")))
            chat = self.repo.get("Chats", ep.chat_id) or {}
            if analysis.kind in ("issue", "context"):
                learn_profile(self.repo, ep, analysis)
            outcome, issue_id = apply_analysis(self.repo, ep, analysis, chat.get("default_platform", ""))
            res.episodes += 1
            if issue_id:
                touched.add(issue_id)
            if outcome == "new":
                res.new_issues += 1
                res.new_ids.append(issue_id)
                catalog = self._catalog()
            elif outcome in ("signal", "context", "old_version"):
                res.signals += 1
            elif outcome == "noise":
                res.filtered += len(ep.messages)
            elif outcome == "review":
                res.review += len(ep.messages)
            if n % 10 == 0:
                for iid in touched:
                    recount_issue(self.repo, iid)
                await self.repo.commit()
        for iid in touched | edited_from:
            recount_issue(self.repo, iid)
        self._reject_emptied(edited_from - touched)
        await self._check_limit_alert()
        audit(self.repo, "AI", res.run_id, "Анализ",
              "", f"новых {res.new_issues}, сигналов {res.signals}, отфильтровано {res.filtered}, на проверке {res.review}")
