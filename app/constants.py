"""Справочники и схема таблицы (раздел 10 ТЗ)."""
from __future__ import annotations

TYPES = ["Баг", "Пожелание", "UX", "Вопрос"]
PRIORITIES = ["Критичный", "Высокий", "Средний", "Низкий"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
PLATFORMS = ["iOS", "Android", "iOS и Android", "Не указана"]
OWNERS = ["Не назначен", "QA", "Android", "iOS", "Backend", "BA", "DevOps"]
NO_OWNER = "Не назначен"
NO_PLATFORM = "Не указана"
BOTH_PLATFORMS = "iOS и Android"

BUG_STATUSES = ["Новое", "Уточнение", "Принято", "В работе", "Устранено", "Закрыто", "Отклонено"]
WISH_STATUSES = ["Новое", "Уточнение", "Оценка", "В бэклоге", "Реализовано", "Отклонено"]
QUESTION_STATUSES = ["Новое", "Отвечено"]
STATUSES = {
    "Баг": BUG_STATUSES,
    "Пожелание": WISH_STATUSES,
    "UX": WISH_STATUSES,
    "Вопрос": QUESTION_STATUSES,
}
RESOLVED_STATUSES = {"Устранено", "Закрыто", "Реализовано"}
DONE_STATUSES = RESOLVED_STATUSES | {"Отвечено"}
TERMINAL_STATUSES = DONE_STATUSES | {"Отклонено"}
REJECT_REASONS = ["Дубль", "Не воспроизводится", "Ожидаемое поведение", "Не относится к приложению"]

# Состояния сообщения
MSG_QUEUED = "В очереди"
MSG_ACCOUNTED = "Учтено"
MSG_CONTEXT = "Контекст"
MSG_REVIEW = "На проверке"
MSG_FILTERED = "Отфильтровано"
MESSAGE_STATES = [MSG_QUEUED, MSG_ACCOUNTED, MSG_CONTEXT, MSG_REVIEW, MSG_FILTERED]
LINKED_STATES = {MSG_ACCOUNTED, MSG_CONTEXT}

# Связи между обращениями
REL_REGRESSION = "Регрессия"
REL_SIMILAR = "Похожее — проверить"
REL_MERGED = "Объединено"

SECTIONS = [
    "Переводы",
    "Платежи",
    "Вклады",
    "Карты",
    "Кредиты",
    "Конвертация",
    "Вход и регистрация",
    "Главная",
    "Уведомления",
    "Профиль и настройки",
    "Прочее",
]
OTHER_SECTION = "Прочее"
UNKNOWN_OPERATION = "Не определена"

# Стартовый справочник операций: (раздел, операция, ключевые слова RU / UZ лат. / UZ кир.)
DEFAULT_OPERATIONS = [
    ("Переводы", "Перевод на карту", "перевод на карту, p2p, на карту, kartaga, o'tkazma, otkazma, ўтказма, картага"),
    ("Переводы", "Перевод по номеру телефона", "по номеру, по телефону, telefon raqam, raqam orqali"),
    ("Переводы", "Международный перевод", "международный, за рубеж, swift, xalqaro"),
    ("Переводы", "История переводов", "история переводов, повторить перевод, tarix"),
    ("Платежи", "Оплата мобильной связи", "мобильная связь, оплата телефона, баланс телефона, mobil aloqa, uzmobile, beeline, ucell, mobiuz"),
    ("Платежи", "Коммунальные платежи", "коммунальные, газ, свет, вода, kommunal"),
    ("Платежи", "Интернет и ТВ", "интернет, тв, internet"),
    ("Платежи", "Чек об оплате", "чек, квитанция, chek, kvitansiya"),
    ("Вклады", "Оформление вклада", "открыть вклад, оформление вклада, omonat ochish, depozit"),
    ("Вклады", "Пополнение вклада", "пополнить вклад, пополнение вклада, omonatni to'ldirish"),
    ("Вклады", "Доходность и начисления", "доходность, проценты, начисления, foiz"),
    ("Карты", "Просмотр карты", "открыть карту, реквизиты карты, karta, карта humo, карта uzcard"),
    ("Карты", "Блокировка карты", "заблокировать карту, блокировка, bloklash"),
    ("Карты", "Выпуск карты", "выпуск карты, заказать карту, виртуальная карта"),
    ("Кредиты", "Погашение кредита", "погасить кредит, погашение, kredit to'lash"),
    ("Кредиты", "Заявка на кредит", "заявка на кредит, кредит, микрозайм, kredit"),
    ("Конвертация", "Обмен валюты", "конвертация, обмен валюты, курс, valyuta, kurs"),
    ("Вход и регистрация", "Вход в приложение", "вход, войти, логин, pin, пин-код, face id, kirish"),
    ("Вход и регистрация", "Регистрация", "регистрация, ro'yxatdan o'tish, код подтверждения, sms-код"),
    ("Главная", "Главный экран", "главная, главный экран, баланс, bosh sahifa"),
    ("Уведомления", "Push-уведомления", "push, пуш, уведомление, bildirishnoma"),
    ("Профиль и настройки", "Язык и настройки", "язык, настройки, til, sozlamalar"),
]

# Схема таблицы. Первый элемент — ключевые поля записи.
SCHEMA: dict[str, dict] = {
    "Participants": {
        "key": ["telegram_id"],
        "cols": ["telegram_id", "name", "username", "platform", "device", "app_version", "updated_at"],
    },
    "Chats": {
        "key": ["chat_id"],
        "cols": ["chat_id", "title", "default_platform", "active", "added_at"],
    },
    "Operations": {
        "key": ["section", "operation"],
        "cols": ["section", "operation", "keywords", "status", "merged_into", "created_at"],
    },
    "Releases": {
        "key": ["version", "platform"],
        "cols": ["version", "platform", "released_at", "created_by"],
    },
    "Settings": {"key": ["key"], "cols": ["key", "value"]},
    "Admins": {"key": ["telegram_id"], "cols": ["telegram_id", "name"]},
    "Messages": {
        "key": ["msg_id"],
        "cols": [
            "msg_id", "chat_id", "chat_title", "message_id", "thread_id", "reply_to", "media_group_id",
            "author_id", "author_name", "sent_at", "edited_at", "kind", "platform", "device", "app_version",
            "text_masked", "screens", "screen_status", "raw_files", "mask_summary", "link", "episode_id",
            "state", "state_reason", "state_by", "issue_id", "updated_at", "also_issues",
        ],
    },
    "Issues": {
        "key": ["issue_id"],
        "cols": [
            "issue_id", "created_at", "updated_at", "type_ai", "type", "section", "operation", "platform",
            "device", "app_version", "priority_ai", "priority", "status", "title", "summary", "steps",
            "actual", "expected", "error_text", "reason_ai", "confidence", "owner", "accepted_by",
            "accepted_at", "due", "due_set_by", "resolved_at", "fix_version", "resolution", "reject_reason",
            "jira_key", "jira_text", "comment", "msg_count", "participant_count", "authors", "chats",
            "merged_into", "screen_unverified",
        ],
    },
    "Links": {
        "key": ["issue_id", "related_id", "relation"],
        "cols": ["issue_id", "related_id", "relation", "created_by", "created_at"],
    },
    "Audit": {"key": None, "cols": ["ts", "actor", "object_id", "field", "old", "new"]},
    "Runs": {
        "key": ["run_id"],
        "cols": [
            "run_id", "started_at", "finished_at", "trigger", "messages", "episodes", "new_issues",
            "signals", "filtered", "review", "errors", "claude_calls",
        ],
    },
}

# Порядок записи: сообщения раньше обращений (восстановление после сбоя, раздел 7 ТЗ).
COMMIT_ORDER = [
    "Participants", "Chats", "Operations", "Releases", "Settings", "Admins",
    "Messages", "Issues", "Links", "Audit", "Runs",
]

DEFAULT_SETTINGS = {
    "auto_analysis": "1",
    "filter_greetings": "1",
    "filter_organization": "1",
    "filter_reactions": "1",
}
