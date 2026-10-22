"""Обработчики Telegram: приём из чатов, регистрация участников, команды админа."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart, ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..config import Config
from ..pipeline.analyzer import Analyzer
from ..services import audit
from ..storage.repo import Repo
from ..util import iso, parse_version
from .collector import Collector

log = logging.getLogger(__name__)
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
SOURCES = GROUPS | {ChatType.CHANNEL}
VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


class LoginTokens:
    """Одноразовые ссылки входа админа на дашборд (команда /login)."""

    TTL = 600

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[int, str, float]] = {}

    def issue(self, user_id: int, name: str) -> str:
        now = time.time()
        self._tokens = {t: v for t, v in self._tokens.items() if v[2] > now}
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (user_id, name, now + self.TTL)
        return token

    def consume(self, token: str) -> tuple[int, str] | None:
        item = self._tokens.pop(token, None)
        if not item or item[2] < time.time():
            return None
        return item[0], item[1]


class Registration(StatesGroup):
    device = State()
    version = State()


def is_admin(repo: Repo, cfg: Config, user_id: int | None, username: str | None = None) -> bool:
    """Команды бота — только для администраторов: ADMIN_IDS, ADMIN_USERNAMES или вкладка Admins."""
    if user_id is None:
        return False
    if username and username.lower() in cfg.admin_usernames:
        return True
    return user_id in cfg.admin_ids or bool(repo.get("Admins", str(user_id)))


def user_is_admin(repo: Repo, cfg: Config, user) -> bool:
    return bool(user) and is_admin(repo, cfg, user.id, user.username)


def admin_ids(repo: Repo, cfg: Config) -> set[int]:
    ids = set(cfg.admin_ids)
    for rec in repo.rows("Admins"):
        if rec.get("telegram_id", "").lstrip("-").isdigit():
            ids.add(int(rec["telegram_id"]))
    return ids


def platform_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="iOS", callback_data="reg:iOS"),
        InlineKeyboardButton(text="Android", callback_data="reg:Android"),
    ]])


HELP = (
    "Focus Pulse собирает обратную связь фокус-группы мобильного приложения.\n\n"
    "Участникам ничего делать не нужно: бот сам собирает сообщения из чатов фокус-группы.\n"
    "По желанию:\n"
    "/start — указать платформу, модель телефона и версию приложения (уточняет разбор)\n"
    "/version 4.2.1 — обновить версию приложения\n"
    "/profile — посмотреть свой профиль\n\n"
    "Пожалуйста, не пишите в чат ФИО клиентов: в свободном тексте их не всегда удаётся скрыть.\n\n"
    "Администраторам:\n"
    "/connect — в чате фокус-группы: подключить чат\n"
    "/disconnect — отключить чат\n"
    "/login — ссылка для входа на дашборд\n"
    "/run — запустить анализ сейчас"
)


def build_router(repo: Repo, cfg: Config, collector: Collector, analyzer: Analyzer, tokens: LoginTokens) -> Router:
    router = Router()

    def admin_only(event) -> bool:
        """Личные команды бота отвечают только администратору; остальным бот молчит."""
        return user_is_admin(repo, cfg, getattr(event, "from_user", None))

    # ------------------------------------------------------------ группы
    @router.message(Command("connect"), F.chat.type.in_(GROUPS))
    async def connect(message: Message) -> None:
        if not user_is_admin(repo, cfg, message.from_user):
            return
        chat_id = str(message.chat.id)
        rec = repo.get("Chats", chat_id)
        if rec:
            old = repo.update("Chats", chat_id, {"active": "1", "title": message.chat.title or rec.get("title", "")})
        else:
            repo.insert("Chats", {"chat_id": chat_id, "title": message.chat.title or "", "default_platform": "",
                                  "active": "1", "added_at": iso()})
            old = {"active": ""}
        audit(repo, _actor(message), chat_id, "Активен", old.get("active", "1"), "1")
        await repo.commit()
        await message.reply("Чат подключён к Focus Pulse. Сообщения с этого момента попадают в анализ.")

    @router.message(Command("disconnect"), F.chat.type.in_(GROUPS))
    async def disconnect(message: Message) -> None:
        if not user_is_admin(repo, cfg, message.from_user):
            return
        chat_id = str(message.chat.id)
        if repo.get("Chats", chat_id):
            repo.update("Chats", chat_id, {"active": "0"})
            audit(repo, _actor(message), chat_id, "Активен", "1", "0")
            await repo.commit()
        await message.reply("Чат отключён. Новые сообщения не собираются.")

    @router.message(F.chat.type.in_(GROUPS))
    async def group_message(message: Message) -> None:
        if message.text and message.text.startswith("/") and message.text.split()[0].split("@")[0] in {"/start", "/help"}:
            return
        collector.store_message(message)

    @router.edited_message(F.chat.type.in_(GROUPS))
    async def group_edit(message: Message) -> None:
        collector.store_edit(message)

    # Каналы: бот должен быть админом канала, подключается автоматически, если его добавил админ.
    @router.channel_post()
    async def channel_post(message: Message) -> None:
        collector.store_message(message)

    @router.edited_channel_post()
    async def channel_edit(message: Message) -> None:
        collector.store_edit(message)

    @router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
    async def bot_removed(event: ChatMemberUpdated) -> None:
        """Бота удалили из группы или канала — чат пропадает из источников, сбор выключается."""
        await mark_chat_removed(repo, str(event.chat.id), f"{event.from_user.full_name} ({event.from_user.id})")

    @router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
    async def bot_added(event: ChatMemberUpdated, bot: Bot) -> None:
        if event.chat.type not in SOURCES:
            return
        chat_id = str(event.chat.id)
        by_admin = user_is_admin(repo, cfg, event.from_user)
        rec = repo.get("Chats", chat_id)
        if not rec:
            repo.insert("Chats", {"chat_id": chat_id, "title": event.chat.title or "", "default_platform": "",
                                  "active": "1" if by_admin else "0", "added_at": iso()})
        elif by_admin:
            repo.update("Chats", chat_id, {"active": "1", "removed_at": "", "title": event.chat.title or rec.get("title", "")})
        else:
            repo.update("Chats", chat_id, {"removed_at": ""})
        if by_admin:
            audit(repo, f"{event.from_user.full_name} ({event.from_user.id})", chat_id, "Активен", "", "1")
        await repo.commit()
        hint = ("Если бот не видит обычные сообщения, отключите privacy mode в BotFather (/setprivacy → Disable) "
                "и добавьте бота заново либо сделайте его админом чата.")
        if by_admin:
            text = f"Чат «{event.chat.title}» подключён: сообщения с этого момента попадают в анализ. {hint}"
        else:
            text = (f"Бот добавлен в чат «{event.chat.title}» не администратором. Чтобы собирать сообщения, "
                    f"отправьте в чате /connect. {hint}")
        for uid in admin_ids(repo, cfg):
            try:
                await bot.send_message(uid, text)
            except Exception:
                log.info("Не удалось уведомить админа %s", uid)

    # ------------------------------------------------------------ личка: регистрация
    @router.message(CommandStart(), F.chat.type == ChatType.PRIVATE, admin_only)
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        _ensure_participant(repo, message)
        await repo.commit()
        await message.answer(
            "Здравствуйте! Это бот фокус-группы мобильного приложения. Он сам собирает сообщения из чатов — "
            "регистрироваться не обязательно.\n"
            "Если хотите, укажите платформу, на которой тестируете приложение, — это поможет точнее разбирать ваши сообщения:",
            reply_markup=platform_keyboard(),
        )

    @router.callback_query(F.data.startswith("reg:"), admin_only)
    async def reg_platform(cb: CallbackQuery, state: FSMContext) -> None:
        platform = cb.data.split(":", 1)[1]
        if platform not in ("iOS", "Android"):
            await cb.answer()
            return
        uid = str(cb.from_user.id)
        if not repo.get("Participants", uid):
            repo.insert("Participants", {"telegram_id": uid, "name": cb.from_user.full_name,
                                         "username": cb.from_user.username or ""})
        repo.update("Participants", uid, {"platform": platform, "updated_at": iso()})
        await repo.commit()
        await state.set_state(Registration.device)
        await cb.message.answer(f"Платформа: {platform}. Модель телефона? Например: Samsung A54 или iPhone 13")
        await cb.answer()

    @router.message(Registration.device, F.chat.type == ChatType.PRIVATE, F.text, admin_only)
    async def reg_device(message: Message, state: FSMContext) -> None:
        device = message.text.strip()[:60]
        repo.update("Participants", str(message.from_user.id), {"device": device, "updated_at": iso()})
        await repo.commit()
        await state.set_state(Registration.version)
        await message.answer("Версия приложения? Её видно в «Профиль → О приложении». Например: 4.2.0")

    @router.message(Registration.version, F.chat.type == ChatType.PRIVATE, F.text, admin_only)
    async def reg_version(message: Message, state: FSMContext) -> None:
        version = message.text.strip()
        if not VERSION_RE.match(version):
            await message.answer("Не похоже на номер версии. Введите в формате 4.2.0")
            return
        repo.update("Participants", str(message.from_user.id), {"app_version": version, "updated_at": iso()})
        await repo.commit()
        await state.clear()
        await message.answer("Готово, профиль сохранён. Когда обновите приложение, отправьте /version 4.2.1\n\n" + _profile(repo, message.from_user.id))

    @router.message(Command("version"), F.chat.type == ChatType.PRIVATE, admin_only)
    async def set_version(message: Message, command: CommandObject) -> None:
        version = (command.args or "").strip()
        if not VERSION_RE.match(version):
            await message.answer("Укажите версию: /version 4.2.1")
            return
        _ensure_participant(repo, message)
        repo.update("Participants", str(message.from_user.id), {"app_version": version, "updated_at": iso()})
        await repo.commit()
        await message.answer(f"Версия обновлена: {version}")

    @router.message(Command("profile"), F.chat.type == ChatType.PRIVATE, admin_only)
    async def profile(message: Message) -> None:
        await message.answer(_profile(repo, message.from_user.id))

    @router.message(Command("help"), admin_only)
    async def help_cmd(message: Message) -> None:
        if message.chat.type == ChatType.PRIVATE:
            await message.answer(HELP)

    # ------------------------------------------------------------ личка: админ
    @router.message(Command("login"), F.chat.type == ChatType.PRIVATE, admin_only)
    async def login(message: Message) -> None:
        if not user_is_admin(repo, cfg, message.from_user):
            await message.answer("Вход на дашборд доступен только администраторам.")
            return
        if not cfg.dashboard_url:
            await message.answer("Не задан DASHBOARD_URL — не могу сформировать ссылку.")
            return
        token = tokens.issue(message.from_user.id, message.from_user.full_name)
        await message.answer(
            f"Ссылка для входа (одноразовая, 10 минут):\n{cfg.dashboard_url}/auth/link?t={token}",
            disable_web_page_preview=True,
        )

    @router.message(Command("run"), F.chat.type == ChatType.PRIVATE, admin_only)
    async def run_now(message: Message) -> None:
        if not user_is_admin(repo, cfg, message.from_user):
            return
        if analyzer.running:
            await message.answer("Анализ уже идёт.")
            return
        await message.answer("Запускаю анализ…")
        await repo.commit()
        res = await analyzer.run("telegram", force=True)
        if res:
            await message.answer(
                f"Готово: новых обращений {res.new_issues}, сигналов {res.signals}, "
                f"отфильтровано {res.filtered}, на проверке {res.review}, ошибок {res.errors}."
            )

    return router


async def mark_chat_removed(repo: Repo, chat_id: str, actor: str) -> None:
    """Чат без сообщений удаляется из таблицы; с сообщениями — остаётся для истории обращений, но скрыт."""
    rec = repo.get("Chats", chat_id)
    if not rec or rec.get("removed_at"):
        return
    if any(m.get("chat_id") == chat_id for m in repo.rows("Messages")):
        repo.update("Chats", chat_id, {"active": "0", "removed_at": iso()})
        audit(repo, actor, chat_id, "Бот удалён из чата", rec.get("title", ""), "")
        await repo.commit()
    else:
        await repo.delete_where({"Chats": lambda r: r.get("chat_id") == chat_id})
    log.info("Бот удалён из чата %s (%s)", chat_id, rec.get("title", ""))


async def sync_chats(bot: Bot, repo: Repo) -> None:
    """При запуске: чаты, из которых бота удалили, пока сервис не работал, — тоже убрать из источников."""
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    me = await bot.get_me()
    for rec in list(repo.rows("Chats")):
        if rec.get("removed_at"):
            continue
        try:
            member = await bot.get_chat_member(int(rec["chat_id"]), me.id)
            gone = member.status in ("left", "kicked")
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            gone = "not found" in str(exc).lower() or isinstance(exc, TelegramForbiddenError)
        except Exception:
            log.warning("Не удалось проверить чат %s", rec["chat_id"], exc_info=True)
            continue
        if gone:
            await mark_chat_removed(repo, rec["chat_id"], "Проверка при запуске")


def _actor(message: Message) -> str:
    u = message.from_user
    return f"{u.full_name} ({u.id})" if u else "Telegram"


def _ensure_participant(repo: Repo, message: Message) -> None:
    u = message.from_user
    if not u:
        return
    uid = str(u.id)
    if not repo.get("Participants", uid):
        repo.insert("Participants", {"telegram_id": uid, "name": u.full_name, "username": u.username or "",
                                     "platform": "", "device": "", "app_version": "", "updated_at": iso()})
    else:
        repo.update("Participants", uid, {"name": u.full_name, "username": u.username or ""})


def _profile(repo: Repo, user_id: int) -> str:
    rec = repo.get("Participants", str(user_id)) or {}
    return (f"Платформа: {rec.get('platform') or 'не указана'}\n"
            f"Телефон: {rec.get('device') or 'не указан'}\n"
            f"Версия приложения: {rec.get('app_version') or 'не указана'}")


async def notify_release(bot: Bot, repo: Repo, version: str, platform: str) -> int:
    """Просит участников обновить версию в профиле после выхода релиза."""
    sent = 0
    for rec in repo.rows("Participants"):
        if platform in ("iOS", "Android") and rec.get("platform") not in ("", platform):
            continue
        current = rec.get("app_version", "")
        if current and parse_version(current) and parse_version(current) >= (parse_version(version) or ()):
            continue
        try:
            await bot.send_message(
                int(rec["telegram_id"]),
                f"Вышла версия {version}{' для ' + platform if platform in ('iOS', 'Android') else ''}. "
                f"Обновите приложение и отправьте /version {version}",
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            log.info("Не удалось написать участнику %s", rec.get("telegram_id"))
    return sent
