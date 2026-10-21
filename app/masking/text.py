"""Обезличивание текста (раздел 6 ТЗ).

Правила применяются до любой отправки в Claude, записи в таблицу и показа на дашборде.
Суммы не маскируются. Имена авторов сообщений (сотрудников) не маскируются.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

DOT = "•"

# Буквы, допустимые в именах (кириллица, узбекская латиница и кириллица).
_UP = "A-ZА-ЯЁЎҚҒҲ"
_LO = "a-zа-яёўқғҳ"
_APOS = "'ʻʼ’‘`"
NAME_WORD = rf"(?:[{_UP}][{_LO}{_APOS}\-]+|[{_UP}{_APOS}]{{2,}})"
NAME_SEQ = rf"{NAME_WORD}(?:[ \t]+{NAME_WORD}){{0,2}}"

FIO_LABELS = (
    r"Получатель|Отправитель|Владелец\s+карты|Владелец|Держатель\s+карты|ФИО|Ф\.\s?И\.\s?О\.?"
    r"|Qabul\s+qiluvchi|Jo['ʻ’‘`]?natuvchi|Karta\s+egasi|F\.\s?I\.\s?Sh\.?|F\.\s?I\.\s?O\.?"
    r"|Қабул\s+қилувчи|Жўнатувчи|Карта\s+эгаси|Ф\.\s?И\.\s?Ш\.?"
)

ACCOUNT_RE = re.compile(r"(?<!\d)\d{20}(?!\d)")
PINFL_RE = re.compile(r"(?<!\d)\d{14}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(\d{4})([ \-]?)(\d{4})\2(\d{4})\2(\d{4})(?!\d)")
PHONE_INTL_RE = re.compile(
    r"(?<![\d])(?:\+\s?)?998[\s\-]?\(?(\d{2})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})(?!\d)"
)
PHONE_LOCAL_RE = re.compile(r"(?<![\d+])\(?(\d{2})\)?[ \-](\d{3})[ \-](\d{2})[ \-](\d{2})(?!\d)")
PHONE_KEYWORD_RE = re.compile(
    r"(?i)(?P<label>\b(?:тел(?:ефон)?|номер(?:\s+телефона)?|telefon|raqam(?:i)?|телефон\s+рақам)\b[^\d\n]{0,12})"
    r"(?<!\d)(?P<num>\d{9})(?!\d)"
)
PASSPORT_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(?:[A-Z]{2}|[А-ЯЁ]{2})[ ]?\d{7}(?!\d)")
INN_RE = re.compile(r"(?i)(?P<label>\b(?:ИНН|INN|STIR|СТИР)\b\s*[:№#]?\s*)(?<!\d)(?P<value>\d{9})(?!\d)")
SMS_RE = re.compile(
    r"(?i)(?P<label>\b(?:код\w*|kod\w*|parol\w*|пароль|code|sms|смс)\b"
    r"(?!\s*(?:ошибк|error|xato|хато|операц|транзакц))[^\d\n]{0,20}?)(?<!\d)(?P<value>\d{4,6})(?!\d)"
)
CVV_RE = re.compile(r"(?i)(?P<label>\b(?:cvv2?|cvc2?)\b\s*[:\-]?\s*)(?P<value>\d{3})(?!\d)")
EXPIRY_RE = re.compile(r"(?<![\d/.])(0[1-9]|1[0-2])\s?/\s?\d{2}(?![\d/])")
EXPIRY_HINT_RE = re.compile(
    r"(?i)срок\s+действия|действует\s+до|amal\s+qilish|valid|exp(?:iry|ires)?\b|муддат"
)
FIO_RE = re.compile(rf"(?P<label>(?i:\b(?:{FIO_LABELS}))\s*[:\-–—]?[ \t]*)(?P<name>{NAME_SEQ})")
FIO_LABEL_ONLY_RE = re.compile(rf"(?i:^\s*(?:{FIO_LABELS})\s*[:\-–—]?\s*$)")

LABELS = {
    "card": "карта",
    "phone": "телефон",
    "account": "счёт",
    "pinfl": "ПИНФЛ",
    "passport": "паспорт",
    "inn": "ИНН",
    "code": "код из SMS",
    "cvv": "CVV",
    "expiry": "срок карты",
    "fio": "ФИО",
    "digits": "длинное число",
}


@dataclass
class MaskResult:
    text: str
    counts: Counter = field(default_factory=Counter)
    spans: list[tuple[int, int, str]] = field(default_factory=list)  # позиции в ИСХОДНОМ тексте

    @property
    def summary(self) -> str:
        return summarize(self.counts)

    @property
    def found(self) -> bool:
        return sum(self.counts.values()) > 0


def summarize(counts: Counter | dict) -> str:
    parts = [f"{LABELS.get(k, k)} ×{n}" for k, n in counts.items() if n]
    return ", ".join(parts)


def merge_summaries(*summaries: str) -> str:
    total: Counter = Counter()
    rev = {v: k for k, v in LABELS.items()}
    for s in summaries:
        for part in (s or "").split(","):
            part = part.strip()
            if "×" not in part:
                continue
            label, _, num = part.rpartition("×")
            key = rev.get(label.strip(), label.strip())
            try:
                total[key] += int(num)
            except ValueError:
                continue
    return summarize(total)


def _card_mask(m: re.Match) -> str:
    digits = "".join(m.group(1, 3, 4, 5))
    return f"{digits[:4]} {digits[4:6]}{DOT * 2} {DOT * 4} {digits[-4:]}"


def _phone_intl_mask(m: re.Match) -> str:
    return f"+998 {m.group(1)} {DOT * 3} {DOT * 2} {m.group(4)}"


def _phone_local_mask(m: re.Match) -> str:
    return f"{m.group(1)} {DOT * 3} {DOT * 2} {m.group(4)}"


class _Masker:
    """Последовательно применяет правила, запоминая исходные позиции совпадений."""

    def __init__(self, text: str):
        self.original = text
        self.counts: Counter = Counter()
        self.spans: list[tuple[int, int, str]] = []
        # (start, end, replacement, kind) по исходному тексту
        self._repl: list[tuple[int, int, str, str]] = []

    def _free(self, start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e, _, _ in self._repl)

    def apply(self, regex: re.Pattern, kind: str, repl, group: str | int = 0) -> None:
        for m in regex.finditer(self.original):
            start, end = m.span(group)
            if start == end or not self._free(start, end):
                continue
            if callable(repl):
                value = repl(m)
            else:
                value = repl
            self._repl.append((start, end, value, kind))
            self.counts[kind] += 1
            self.spans.append((start, end, kind))

    def result(self) -> MaskResult:
        out = []
        pos = 0
        for start, end, value, _ in sorted(self._repl):
            out.append(self.original[pos:start])
            out.append(value)
            pos = end
        out.append(self.original[pos:])
        return MaskResult("".join(out), self.counts, sorted(self.spans))


def mask_text(text: str, force_expiry: bool = False) -> MaskResult:
    """Маскирует персональные и платёжные данные клиента в тексте."""
    if not text:
        return MaskResult(text or "")
    mk = _Masker(text)
    mk.apply(ACCOUNT_RE, "account", "[СЧЁТ]")
    mk.apply(PINFL_RE, "pinfl", "[ПИНФЛ]")
    mk.apply(CARD_RE, "card", _card_mask)
    mk.apply(PHONE_INTL_RE, "phone", _phone_intl_mask)
    mk.apply(PHONE_LOCAL_RE, "phone", _phone_local_mask)
    mk.apply(PHONE_KEYWORD_RE, "phone", lambda m: f"{m.group('num')[:2]} {DOT * 3} {DOT * 2} {m.group('num')[-2:]}", "num")
    mk.apply(PASSPORT_RE, "passport", "[ПАСПОРТ]")
    mk.apply(INN_RE, "inn", "[ИНН]", "value")
    mk.apply(SMS_RE, "code", "[КОД]", "value")
    mk.apply(CVV_RE, "cvv", "[CVV]", "value")
    if force_expiry or mk.counts.get("card") or EXPIRY_HINT_RE.search(text):
        mk.apply(EXPIRY_RE, "expiry", "[СРОК]")
    mk.apply(FIO_RE, "fio", "[ФИО]", "name")
    return mk.result()


def is_fio_label_line(line: str) -> bool:
    """Строка состоит только из подписи «Получатель:» — имя стоит на следующей строке."""
    return bool(FIO_LABEL_ONLY_RE.match(line or ""))
