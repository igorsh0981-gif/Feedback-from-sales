"""Предфильтр без вызова Claude (раздел 7 ТЗ, шаг 1).

Отсекает приветствия, благодарности, реакции и организационные фразы.
Сообщение со скриншотом или с признаками проблемы никогда не отсекается.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

GREETINGS = [
    "доброе утро", "добрый день", "добрый вечер", "всем привет", "привет", "здравствуйте", "всем добрый",
    "салам", "салом", "ассалому алайкум", "ассаламу алейкум", "assalomu alaykum", "assalomu aleykum",
    "hayrli tong", "xayrli tong", "хайрли тонг", "xayrli kun", "hello", "hi all", "good morning", "коллеги",
    "всем", "друзья", "ребята", "hammaga", "hammaga salom",
]
REACTIONS = [
    "спасибо", "спс", "благодарю", "rahmat", "raxmat", "рахмат", "thanks", "thank you", "ok", "ок", "окей",
    "понял", "поняла", "понятно", "ясно", "принято", "хорошо", "отлично", "супер", "да", "нет", "ага",
    "ha", "xa", "yo'q", "tushunarli", "тушунарли", "zo'r", "+", "++", "+1", "👍", "🙏", "👌", "🔥", "✅",
]
ORGANIZATION = [
    "созвон", "встреча", "встречу", "митинг", "совещание", "zoom", "google meet", "teams", "uchrashuv",
    "учрашув", "во сколько", "перенесли", "перенесём", "обед", "планерка", "планёрка", "дейли", "daily",
    "ссылка на встречу", "kim qatnashadi",
]
PROBLEM_HINTS = [
    "не работает", "ошибк", "не проход", "не приход", "завис", "вылет", "закрыва", "не открыва", "не могу",
    "не получается", "не загруж", "не отображ", "баг", "проблем", "снова", "опять", "не удаётся", "не удается",
    "глюч", "тормоз", "списал", "не вижу", "не видно", "пропал", "неудобно", "непонятно", "добавьте",
    "хотелось бы", "предлагаю", "ishlamay", "ishlamayap", "xato", "хато", "ишламай", "muammo", "муаммо",
    "ochilmay", "очилмай", "taklif", "таклиф", "qo'shing", "noqulay", "tushunarsiz", "чек", "перевод",
    "платёж", "платеж", "карт", "вклад", "кредит", "оплат",
]

_EMOJI_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


@dataclass
class Noise:
    category: str  # greetings | reactions | organization
    reason: str


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", text).strip()


def _strip_symbols(text: str) -> str:
    return _EMOJI_PUNCT.sub(" ", text).strip()


def _remove_phrases(text: str, phrases: list[str]) -> str:
    out = f" {text} "
    for p in sorted(phrases, key=len, reverse=True):
        p = _norm(p).replace("ё", "е")
        if not p:
            continue
        if re.fullmatch(r"[\w' ]+", p):
            out = re.sub(rf"(?<![\w']){re.escape(p)}(?![\w'])", " ", out)
        else:
            out = out.replace(p, " ")
    return re.sub(r"\s+", " ", out).strip()


def has_problem_hint(text: str) -> bool:
    t = _norm(text)
    return any(h in t for h in PROBLEM_HINTS)


def detect_noise(text: str, kind: str, has_media: bool, prefs: dict[str, bool]) -> Noise | None:
    """None — сообщение идёт в анализ. Иначе категория шума и причина."""
    if has_media:
        return None
    if kind == "sticker":
        return Noise("reactions", "Стикер без текста") if prefs.get("reactions", True) else None
    t = _norm(text)
    if not t:
        if kind in ("text", "other"):
            return Noise("reactions", "Служебное или пустое сообщение Telegram (без текста и скриншота)")
        return None  # файл без подписи — на проверку через анализ
    if has_problem_hint(t):
        return None
    letters = _strip_symbols(t)
    if not letters:
        return Noise("reactions", "Только эмодзи или знаки") if prefs.get("reactions", True) else None

    rest_after_greet = _strip_symbols(_remove_phrases(letters, GREETINGS))
    rest_after_all = _strip_symbols(_remove_phrases(rest_after_greet, REACTIONS))
    if rest_after_greet != letters and len(rest_after_all) <= 2:
        if prefs.get("greetings", True):
            return Noise("greetings", "Приветствие без обратной связи по приложению")
        return None
    if len(rest_after_all) <= 2:
        if prefs.get("reactions", True):
            return Noise("reactions", "Короткая реакция без новой информации")
        return None
    if any(o in t for o in ORGANIZATION) and len(t) <= 200:
        if prefs.get("organization", True):
            return Noise("organization", "Организационная переписка, не описывает работу приложения")
        return None
    return None
