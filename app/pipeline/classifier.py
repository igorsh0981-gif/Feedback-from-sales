"""Классификация эпизода (раздел 7 ТЗ, шаг 4).

ClaudeClassifier — рабочий: Claude API, ответ через tool use (строгий JSON).
HeuristicClassifier — правила по ключевым словам: для демо и тестов без API-ключа.
На вход обоим идут только обезличенные тексты и скриншоты.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field

from ..constants import PLATFORMS, PRIORITIES, SECTIONS, TYPES

log = logging.getLogger(__name__)

KINDS = ["issue", "context", "noise", "review"]
RELATIONS = ["same", "repeat", "similar", "none"]


@dataclass
class EpisodeMessage:
    author: str  # «Участник 1»
    sent_at: str
    text: str
    platform: str = ""
    device: str = ""
    app_version: str = ""
    screens_ocr: list[str] = field(default_factory=list)


@dataclass
class EpisodeInput:
    episode_id: str
    chat_title: str
    chat_platform: str
    messages: list[EpisodeMessage]
    images: list[bytes] = field(default_factory=list)
    allow_split: bool = True
    context_before: list[str] = field(default_factory=list)
    reply_texts: list[str] = field(default_factory=list)
    reply_issue: str = ""
    catalog: list[tuple[str, str]] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)


@dataclass
class Analysis:
    kind: str = "review"
    type: str = "Баг"
    section: str = "Прочее"
    operation: str = ""
    operation_is_new: bool = False
    platform: str = "Не указана"
    device: str = ""
    app_version: str = ""
    priority: str = "Средний"
    title: str = ""
    summary: str = ""
    steps: list[str] = field(default_factory=list)
    actual: str = ""
    expected: str = ""
    error_text: str = ""
    match_issue: str = ""
    relation: str = "none"
    confidence: float = 0.0
    reason: str = ""
    parts: list[list[int]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Analysis":
        a = cls()
        a.kind = d.get("kind") if d.get("kind") in KINDS else "review"
        a.type = d.get("type") if d.get("type") in TYPES else "Баг"
        a.section = d.get("section") if d.get("section") in SECTIONS else "Прочее"
        a.operation = str(d.get("operation") or "").strip()[:80]
        a.operation_is_new = bool(d.get("operation_is_new"))
        a.platform = d.get("platform") if d.get("platform") in PLATFORMS else "Не указана"
        a.device = str(d.get("device") or "").strip()[:60]
        a.app_version = str(d.get("app_version") or "").strip()[:20]
        a.priority = d.get("priority") if d.get("priority") in PRIORITIES else "Средний"
        a.title = str(d.get("title") or "").strip()[:80]
        a.summary = str(d.get("summary") or "").strip()[:2000]
        steps = d.get("steps") or []
        if isinstance(steps, str):
            steps = [s for s in steps.splitlines() if s.strip()]
        a.steps = [str(s).strip()[:300] for s in steps][:10]
        a.actual = str(d.get("actual") or "").strip()[:1000]
        a.expected = str(d.get("expected") or "").strip()[:1000]
        a.error_text = str(d.get("error_text") or "").strip()[:300]
        m = re.search(r"FP-\d+", str(d.get("match_issue") or ""))
        a.match_issue = m.group(0) if m else ""
        a.relation = d.get("relation") if d.get("relation") in RELATIONS else "none"
        try:
            a.confidence = max(0.0, min(1.0, float(d.get("confidence", 0))))
        except (TypeError, ValueError):
            a.confidence = 0.0
        a.reason = str(d.get("reason") or "").strip()[:1500]
        parts = d.get("parts") or []
        if isinstance(parts, list):
            for p in parts[:10]:
                if isinstance(p, list):
                    nums = sorted({int(x) for x in p if isinstance(x, (int, float)) or str(x).strip().isdigit()})
                    if nums:
                        a.parts.append(nums)
        return a


SYSTEM_PROMPT = """Ты — аналитик обратной связи фокус-группы мобильного банковского приложения.
Участники — сотрудники банка, тестирующие приложение. Пишут по-русски и по-узбекски (латиница и кириллица), часто вперемешку.
Тексты и скриншоты уже обезличены: номера карт, телефоны, ФИО заменены метками вроде [ФИО], закрашенные области на скринах — это скрытые данные, их не нужно восстанавливать.

Задача: разобрать ОДИН эпизод (несколько сообщений об одном) и вызвать инструмент submit_analysis.

kind:
- issue — сообщение о проблеме или идее по приложению (баг, пожелание, UX, вопрос о работе приложения).
- context — уточнение к уже известному обращению (подробности, «у меня тоже», ответ на вопрос), без новой проблемы. Укажи match_issue.
- noise — не относится к работе приложения: приветствия, организационные вопросы, болтовня.
- review — неясно, о чём речь, не хватает контекста («у меня снова не работает» без пояснений). Не угадывай.

type:
- Баг — приложение работает не так, как должно: ошибка, сбой, зависание, вылет, неверные данные.
- Пожелание — просьба о новой возможности.
- UX — функция работает, но неудобно, непонятно, плохо читается, трудно найти.
- Вопрос — вопрос о том, как работает функция или правило (лимиты, комиссии), когда нет явного дефекта.

priority:
- Критичный — деньги списаны без зачисления, операция с деньгами не проходит у многих, вход невозможен.
- Высокий — функция не работает, обхода нет.
- Средний — ошибка, но есть обход; заметная UX-проблема.
- Низкий — косметика, текст, мелкие пожелания.

Совпадения (match_issue, relation) — сравни с кандидатами из списка открытых и недавно закрытых обращений:
- same — та же проблема, что в кандидате (тот же симптом в той же операции).
- repeat — участник явно говорит, что проблема вернулась («опять», «снова», «qayta», «yana»), и она совпадает с кандидатом.
- similar — похоже, но не уверен, что это то же самое.
- none — совпадений нет. Не придумывай match_issue, если кандидатов нет.

Правила:
- section — только из списка разделов. operation — из справочника; если подходящей нет, предложи короткое название и поставь operation_is_new=true.
- platform: iOS/Android, если явно следует из текста, скрина (вид интерфейса, статус-бар) или профиля участника; иначе «Не указана».
- app_version — только если указана в тексте, на скрине или в профиле участника.
- device — модель телефона, только если участник её назвал («на Samsung A54», «iPhone 13») или она указана в профиле.
- title — до 80 символов, по-русски, суть проблемы без оценок («Перевод зависает после подтверждения»).
- summary, steps, actual, expected — по-русски, только по тому, что написал участник; не выдумывай шаги. Если шагов нет — оставь пустой список.
- error_text — дословный текст ошибки со скрина или из сообщения, если есть.
- reason — 1–3 предложения по-русски: почему такой тип, приоритет и совпадение.
- confidence — уверенность в kind и type от 0 до 1.
- Контекст беседы до эпизода дан только для понимания; классифицируй сам эпизод.
- parts — если в эпизоде несколько РАЗНЫХ обращений в разных сообщениях (например, баг и отдельное пожелание), раздели номера сообщений [#N] по обращениям: [[1,2],[3]]. Уточнения и скриншоты относи к тому обращению, к которому они относятся. Если обращение одно — оставь parts пустым. Остальные поля тогда заполни по первой группе."""

TOOL = {
    "name": "submit_analysis",
    "description": "Результат разбора эпизода обратной связи.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": KINDS},
            "type": {"type": "string", "enum": TYPES},
            "section": {"type": "string", "enum": SECTIONS},
            "operation": {"type": "string"},
            "operation_is_new": {"type": "boolean"},
            "platform": {"type": "string", "enum": PLATFORMS},
            "device": {"type": "string"},
            "app_version": {"type": "string"},
            "priority": {"type": "string", "enum": PRIORITIES},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "string"}},
            "actual": {"type": "string"},
            "expected": {"type": "string"},
            "error_text": {"type": "string"},
            "match_issue": {"type": "string", "description": "FP-номер кандидата или пустая строка"},
            "relation": {"type": "string", "enum": RELATIONS},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
            "parts": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}},
                      "description": "Группы номеров сообщений, если в эпизоде несколько разных обращений; иначе пусто"},
        },
        "required": ["kind", "type", "section", "priority", "title", "relation", "confidence", "reason"],
    },
}


def render_episode(ep: EpisodeInput) -> str:
    parts = [f"Чат: {ep.chat_title or 'без названия'}; платформа чата по умолчанию: {ep.chat_platform or 'не задана'}"]
    sections: dict[str, list[str]] = {}
    for sec, op in ep.catalog:
        sections.setdefault(sec, []).append(op)
    parts.append("Разделы и операции:\n" + "\n".join(
        f"- {sec}: {', '.join(ops) if ops else '—'}" for sec, ops in ((s, sections.get(s, [])) for s in SECTIONS)
    ))
    if ep.candidates:
        lines = []
        for c in ep.candidates:
            extra = []
            if c.get("fix_version"):
                extra.append(f"исправлено в {c['fix_version']}")
            if c.get("error_text"):
                extra.append(f"ошибка «{c['error_text'][:80]}»")
            lines.append(
                f"- {c['issue_id']} [{c['type']}; {c['section']} / {c.get('operation') or '—'}; {c['status']}; "
                f"{c.get('platform') or '—'}; версия {c.get('app_version') or '—'}] {c['title']}"
                + (f" ({'; '.join(extra)})" if extra else "")
            )
        parts.append("Кандидаты для совпадения:\n" + "\n".join(lines))
    else:
        parts.append("Кандидаты для совпадения: нет")
    if ep.reply_issue:
        parts.append(f"Эпизод — ответ на сообщение, уже учтённое в обращении {ep.reply_issue}.")
    if ep.reply_texts:
        parts.append("Сообщение, на которое отвечают:\n" + "\n".join(f"> {t[:500]}" for t in ep.reply_texts[:3]))
    if ep.context_before:
        parts.append("Контекст беседы до эпизода (не анализировать):\n" + "\n".join(f"· {t[:300]}" for t in ep.context_before))
    msg_lines = []
    for n, m in enumerate(ep.messages, 1):
        profile = ", ".join(x for x in [m.platform, m.device, f"версия {m.app_version}" if m.app_version else ""] if x)
        head = f"[#{n}] [{m.sent_at[11:16]}] {m.author}" + (f" ({profile})" if profile else "")
        body = m.text or "(без текста)"
        ocr = "".join(f"\n  Текст со скрина: {o[:600]}" for o in m.screens_ocr if o)
        msg_lines.append(f"{head}: {body}{ocr}")
    parts.append("ЭПИЗОД:\n" + "\n".join(msg_lines))
    if not ep.allow_split:
        parts.append("Эпизод уже выделен из более крупного разговора как одно обращение — parts не заполняй.")
    if ep.images:
        parts.append(f"Скриншоты эпизода приложены ниже ({len(ep.images)}).")
    return "\n\n".join(parts)


TOOL_REMINDER = "\n\nОтвет дай только вызовом инструмента submit_analysis, без текста вокруг."


def extract_analysis(blocks) -> dict | None:
    """Результат из tool_use, иначе — первый JSON-объект в текстовом ответе."""
    for block in blocks:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == TOOL["name"]:
            data = block.input
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    continue
            if isinstance(data, dict):
                return data
    for block in blocks:
        if getattr(block, "type", "") != "text":
            continue
        text = getattr(block, "text", "") or ""
        start = text.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(data, dict) and "kind" in data:
                            return data
                        break
            start = text.find("{", start + 1)
    return None


class ClaudeClassifier:
    name = "claude"

    def __init__(self, api_key: str, model: str, max_images: int = 4):
        from anthropic import AsyncAnthropic

        self.client = AsyncAnthropic(api_key=api_key, max_retries=3, timeout=90)
        self.model = model
        self.max_images = max_images

    async def classify(self, ep: EpisodeInput) -> Analysis:
        content: list[dict] = [{"type": "text", "text": render_episode(ep)}]
        for img in ep.images[: self.max_images]:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(img).decode()},
            })
        messages: list[dict] = [{"role": "user", "content": content}]
        # Принудительный tool_choice поддерживают не все модели, поэтому auto + явная инструкция,
        # а если инструмент не вызван — разбор JSON из текста или одна повторная просьба.
        for attempt in range(2):
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=[{"type": "text", "text": SYSTEM_PROMPT + TOOL_REMINDER, "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL],
                tool_choice={"type": "auto"},
                messages=messages,
            )
            data = extract_analysis(resp.content)
            if data is not None:
                return Analysis.from_dict(data)
            messages = messages + [
                {"role": "assistant", "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in resp.content
                                                  if getattr(b, "type", "") == "text"] or "—"},
                {"role": "user", "content": "Вызови инструмент submit_analysis с результатом разбора."},
            ]
        raise RuntimeError("Claude не вернул результат разбора")

    async def ping(self) -> None:
        await self.client.models.list(limit=1)


# ---------------------------------------------------------------- эвристика для демо и тестов
BUG_WORDS = ["не работает", "ошибк", "не проход", "не приход", "завис", "вылет", "закрыва", "не открыва",
             "пустой экран", "не реагир", "не обновля", "дважды", "списал", "ishlamay", "xato", "ochilmay", "не пришёл", "не пришел"]
WISH_WORDS = ["добавьте", "хотелось бы", "хочу", "можно ли добавить", "было бы удобно", "предлагаю", "можно кнопку",
              "taklif", "qo'shing", "qo'shsangiz"]
UX_WORDS = ["неудобно", "непонятно", "мелк", "не найти", "где теперь", "трудно", "не понятно", "noqulay", "tushunarsiz"]
REPEAT_WORDS = ["снова", "опять", "ещё раз", "повторно", "qayta", "yana", "ранее это исправляли", "уже исправляли"]


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-яёўқғҳ']{4,}", text.lower())}


class HeuristicClassifier:
    name = "heuristic"

    async def classify(self, ep: EpisodeInput) -> Analysis:
        text = " ".join(m.text for m in ep.messages)
        ocr = " ".join(o for m in ep.messages for o in m.screens_ocr)
        low = (text + " " + ocr).lower()
        a = Analysis(confidence=0.55)
        if not text.strip() and not ocr.strip():
            a.kind, a.reason = "review", "Нет текста для разбора"
            return a
        if any(w in low for w in BUG_WORDS):
            a.type = "Баг"
        elif any(w in low for w in WISH_WORDS):
            a.type = "Пожелание"
        elif any(w in low for w in UX_WORDS):
            a.type = "UX"
        elif "?" in text:
            a.type = "Вопрос"
        else:
            a.kind, a.reason, a.confidence = "review", "Не найдено признаков бага, пожелания или вопроса", 0.3
            return a
        a.kind = "issue"
        best = ("Прочее", "", 0)
        for sec, op in ep.catalog:
            score = sum(1 for w in _tokens(op) if w[:5] in low)
            if score > best[2]:
                best = (sec, op, score)
        sec_hits = [s for s in SECTIONS if s.lower()[:5] in low]
        a.section, a.operation = (best[0], best[1]) if best[2] else ((sec_hits[0], "") if sec_hits else ("Прочее", ""))
        if "iphone" in low or "ios" in low or "айфон" in low:
            a.platform = "iOS"
        elif "android" in low or "samsung" in low or "андроид" in low or "xiaomi" in low:
            a.platform = "Android"
        m = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
        a.app_version = m.group(1) if m else ""
        if a.type == "Баг":
            a.priority = "Критичный" if "списал" in low else ("Высокий" if any(w in low for w in ["вылет", "закрыва", "не открыва", "не приход", "вход"]) else "Средний")
        else:
            a.priority = "Низкий" if a.type == "Пожелание" else "Средний"
        first = ep.messages[0].text.strip() or ocr.strip()
        a.title = re.split(r"[.!?\n]", first)[0][:80] or "Обращение участника"
        a.summary = text.strip()
        a.actual = text.strip() if a.type == "Баг" else ""
        tokens = _tokens(text)
        best_c, best_score = None, 0.0
        for c in ep.candidates:
            ct = _tokens(c["title"] + " " + c.get("operation", ""))
            if not ct or not tokens:
                continue
            score = len(tokens & ct) / len(tokens | ct)
            if c.get("section") == a.section:
                score += 0.1
            if score > best_score:
                best_c, best_score = c, score
        if ep.reply_issue and len(text) < 160 and a.type != "Пожелание":
            a.kind, a.match_issue, a.relation = "context", ep.reply_issue, "same"
            a.reason = f"Ответ в цепочке обращения {ep.reply_issue}; новых сведений о другой проблеме нет."
            return a
        if best_c and best_score >= 0.18:
            a.match_issue = best_c["issue_id"]
            a.relation = "repeat" if any(w in low for w in REPEAT_WORDS) else ("same" if best_score >= 0.3 else "similar")
        a.reason = f"Правила по ключевым словам: тип «{a.type}», раздел «{a.section}»." + (
            f" Похоже на {a.match_issue}." if a.match_issue else "")
        return a

    async def ping(self) -> None:
        return None
