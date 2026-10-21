"""Точка входа: бот (polling) + дашборд + планировщик анализа + heartbeat в одном процессе."""
from __future__ import annotations

import asyncio
import io
import logging
import signal

from aiohttp import web

from .config import Config, load_config
from .constants import DEFAULT_OPERATIONS
from .pipeline.analyzer import Analyzer
from .pipeline.classifier import ClaudeClassifier, HeuristicClassifier
from .storage.backends import MemoryBackend, SheetsBackend
from .storage.repo import Repo
from .util import iso, now
from .web.server import AppContext, Health, create_app

log = logging.getLogger("focus_pulse")
FLUSH_EVERY = 5
HEARTBEAT_EVERY = 3600
ALERT_AFTER = 3


async def seed_operations(repo: Repo) -> None:
    if repo.rows("Operations"):
        return
    for sec, op, kw in DEFAULT_OPERATIONS:
        repo.insert("Operations", {"section": sec, "operation": op, "keywords": kw, "status": "Утверждена",
                                   "created_at": iso()})
    await repo.commit()


class Runtime:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = asyncio.Event()
        self.bot = None
        self.health = Health()
        self.failures = {"sheets": 0, "claude": 0, "telegram": 0}
        self.alerted: set[str] = set()

    async def notify_admins(self, text: str) -> None:
        if not self.bot:
            log.info("notify: %s", text)
            return
        from .bot.handlers import admin_ids

        for uid in admin_ids(self.repo, self.cfg):
            try:
                await self.bot.send_message(uid, text)
            except Exception:
                log.info("Не удалось отправить уведомление админу %s", uid)

    async def download(self, file_id: str) -> bytes:
        buf = io.BytesIO()
        await self.bot.download(file_id, destination=buf)
        return buf.getvalue()

    async def upload_screen(self, data: bytes, caption: str) -> str:
        from aiogram.types import BufferedInputFile

        msg = await self.bot.send_photo(self.cfg.service_channel_id, BufferedInputFile(data, "screen.jpg"),
                                        caption=f"Обезличено · {caption}"[:200])
        return msg.photo[-1].file_id

    # ------------------------------------------------------------ фоновые циклы
    async def scheduler(self) -> None:
        an = self.analyzer
        an.next_run_at = now()
        while not self.stop.is_set():
            wait = max(5.0, (an.next_run_at - now()).total_seconds()) if an.next_run_at else 60.0
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                pass
            if self.repo.setting_on("auto_analysis"):
                try:
                    await self.repo.commit()
                except Exception:
                    log.warning("commit before run failed", exc_info=True)
                res = await an.run("schedule")
                if res is None:
                    an.next_run_at = now()
                    await asyncio.sleep(30)
            else:
                from datetime import timedelta

                an.next_run_at = now() + timedelta(minutes=an.interval_min)

    async def flusher(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=FLUSH_EVERY)
            except asyncio.TimeoutError:
                pass
            if self.repo.pending_changes:
                try:
                    await self.repo.commit()
                except Exception:
                    log.warning("Отложенная запись не удалась — повтор через %s с", FLUSH_EVERY)

    async def heartbeat(self) -> None:
        while not self.stop.is_set():
            await self.check_health()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=HEARTBEAT_EVERY)
            except asyncio.TimeoutError:
                pass

    async def check_health(self) -> None:
        checks = {"sheets": lambda: asyncio.to_thread(self.repo.backend.ping)}
        if self.cfg.claude_enabled:
            checks["claude"] = self.classifier.ping
        if self.bot:
            checks["telegram"] = self.bot.get_me
        for name, fn in checks.items():
            try:
                await fn()
                self.health.status[name] = "ok"
                self.failures[name] = 0
                self.alerted.discard(name)
            except Exception as exc:
                self.failures[name] += 1
                self.health.status[name] = f"ошибка: {type(exc).__name__}"
                log.warning("heartbeat %s failed (%s): %s", name, self.failures[name], exc)
                if self.failures[name] >= ALERT_AFTER and name not in self.alerted:
                    self.alerted.add(name)
                    await self.notify_admins(f"⚠️ Focus Pulse: {name} недоступен {self.failures[name]} проверки подряд: {exc}")
        if not self.cfg.claude_enabled:
            self.health.status["claude"] = "не подключён (правила по словам)"
        if not self.bot:
            self.health.status["telegram"] = "не подключён"
        self.health.checked_at = iso()

    # ------------------------------------------------------------ запуск
    async def run(self) -> None:
        cfg = self.cfg
        if cfg.demo_mode:
            backend = MemoryBackend()
        else:
            if not cfg.sheet_id or not cfg.google_service_account_json:
                raise SystemExit("Нужны SHEET_ID и GOOGLE_SERVICE_ACCOUNT_JSON (или DEMO_MODE=1)")
            backend = await asyncio.to_thread(SheetsBackend, cfg.sheet_id, cfg.google_service_account_json)
        self.repo = Repo(backend)
        await self.repo.init()
        if cfg.demo_mode:
            from .demo import seed_demo

            seed_demo(self.repo)
            await self.repo.commit()
        await seed_operations(self.repo)

        if cfg.claude_enabled:
            self.classifier = ClaudeClassifier(cfg.anthropic_api_key, cfg.claude_model)
        else:
            log.warning("ANTHROPIC_API_KEY не задан — классификация по правилам (только для демо)")
            self.classifier = HeuristicClassifier()

        bot_username = ""
        if cfg.bot_enabled:
            from aiogram import Bot

            self.bot = Bot(cfg.telegram_token)
            me = await self.bot.get_me()
            bot_username = me.username or ""
        if self.bot and not cfg.service_channel_id:
            log.warning("SERVICE_CHANNEL_ID не задан — скриншоты не будут сохраняться и уходить в анализ")

        self.analyzer = Analyzer(
            self.repo, self.classifier, pause_min=cfg.episode_pause_min, daily_limit=cfg.claude_daily_limit,
            image_loader=self.download if self.bot else None, notifier=self.notify_admins,
            interval_min=cfg.analysis_interval_min,
        )
        from .bot.handlers import LoginTokens, is_admin

        tokens = LoginTokens()
        ctx = AppContext(
            repo=self.repo, cfg=cfg, analyzer=self.analyzer, tokens=tokens,
            image_fetcher=self.download if self.bot else None,
            is_admin=lambda uid: is_admin(self.repo, cfg, uid) or (cfg.demo_mode and uid == -1),
            bot_username=bot_username, health=self.health, classifier_name=self.classifier.name,
        )
        if self.bot:
            from .bot.handlers import notify_release

            ctx.release_notifier = lambda v, p: notify_release(self.bot, self.repo, v, p)

        app = create_app(ctx)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", cfg.port)
        await site.start()
        log.info("Дашборд слушает порт %s", cfg.port)

        tasks = [
            asyncio.create_task(self.scheduler(), name="scheduler"),
            asyncio.create_task(self.flusher(), name="flusher"),
            asyncio.create_task(self.heartbeat(), name="heartbeat"),
        ]
        dp = None
        if self.bot:
            from aiogram import Dispatcher
            from aiogram.fsm.storage.memory import MemoryStorage

            from .bot.collector import Collector
            from .bot.handlers import build_router

            collector = Collector(self.repo, cfg.tesseract_langs, self.download,
                                  self.upload_screen if cfg.service_channel_id else None)
            resumed = collector.resume_pending()
            if resumed:
                log.info("Дообработка %s скриншотов после перезапуска", resumed)
            dp = Dispatcher(storage=MemoryStorage())
            dp.include_router(build_router(self.repo, cfg, collector, self.analyzer, tokens))
            await self.bot.delete_webhook(drop_pending_updates=False)
            tasks.append(asyncio.create_task(dp.start_polling(
                self.bot, handle_signals=False, close_bot_session=False,
                allowed_updates=["message", "edited_message", "callback_query", "my_chat_member"],
            ), name="polling"))
            log.info("Бот @%s запущен", bot_username)

        await self.stop.wait()
        log.info("Остановка: сохраняю несохранённые изменения")
        if dp:
            await dp.stop_polling()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.repo.commit()
        except Exception:
            log.exception("Не удалось сохранить изменения при остановке")
        await runner.cleanup()
        if self.bot:
            await self.bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    cfg = load_config()
    rt = Runtime(cfg)

    async def _main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, rt.stop.set)
            except NotImplementedError:
                pass
        await rt.run()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
