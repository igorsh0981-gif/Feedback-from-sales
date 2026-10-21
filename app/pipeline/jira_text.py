"""Текст постановки задачи для ручного переноса в Jira (раздел 12 ТЗ). Без вызова Claude."""
from __future__ import annotations

from ..constants import REL_MERGED
from ..storage.repo import Repo
from ..util import parse_date, short


def _date(value: str) -> str:
    d = parse_date(value)
    return d.strftime("%d.%m.%Y") if d else "не задан"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def related_lines(repo: Repo, issue: dict) -> list[str]:
    out = []
    for link in repo.rows("Links"):
        if link["relation"] == REL_MERGED:
            continue
        other_id = link["related_id"] if link["issue_id"] == issue["issue_id"] else (
            link["issue_id"] if link["related_id"] == issue["issue_id"] else "")
        if not other_id:
            continue
        other = repo.get("Issues", other_id)
        if not other:
            continue
        line = f"{other_id} — {other.get('status', '').lower()}"
        if other.get("fix_version"):
            line += f", исправлено в {other['fix_version']}"
        if link["relation"].startswith("Регрессия"):
            line += f"; повтор на {issue.get('app_version') or 'неизвестной версии'}, возможна регрессия"
        else:
            line += f"; связь: {link['relation'].lower()}"
        out.append(line)
    return out


def build_jira_text(repo: Repo, issue: dict) -> str:
    t = issue.get("type", "")
    if t == "Вопрос":
        return ""
    msgs = int(issue.get("msg_count") or 0)
    people = int(issue.get("participant_count") or 0)
    platform = issue.get("platform") or "Не указана"
    platform_tag = platform if platform != "Не указана" else ""
    tags = f"[{t}]" + (f"[{platform_tag}]" if platform_tag else "")
    title = short(f"{tags} {issue.get('title', '')}", 255)
    source = (f"Источник: {issue['issue_id']} · фокус-группа · {people} {_plural(people, 'участник', 'участника', 'участников')}"
              f" · {msgs} {_plural(msgs, 'сообщение', 'сообщения', 'сообщений')}")
    lines = [title, "", source,
             f"Раздел / операция: {issue.get('section', '')} / {issue.get('operation', '')}"]
    env = [platform]
    if issue.get("device"):
        env.append(issue["device"])
    if issue.get("app_version"):
        env.append(f"приложение {issue['app_version']}")
    lines.append("Окружение: " + " · ".join(env))

    if t == "Баг":
        lines.append(f"Приоритет: {issue.get('priority', '')} · Срок: {_date(issue.get('due', ''))}")
        lines.append("")
        steps = [s.strip() for s in (issue.get("steps") or "").splitlines() if s.strip()]
        if steps:
            lines.append("Шаги воспроизведения:")
            lines += [f"{i}. {s.rstrip('.')}." for i, s in enumerate(steps, 1)]
            lines.append("")
        if issue.get("actual"):
            lines.append(f"Фактический результат: {issue['actual']}")
        if issue.get("expected"):
            lines.append(f"Ожидаемый результат: {issue['expected']}")
        if issue.get("error_text"):
            lines.append(f"Текст ошибки на экране: «{issue['error_text']}».")
        missing = [name for name, val in (("шаги", steps), ("фактический результат", issue.get("actual")),
                                          ("ожидаемый результат", issue.get("expected"))) if not val]
    else:
        target = issue.get("fix_version") or ""
        lines.append(f"Приоритет: {issue.get('priority', '')}" + (f" · Целевой релиз: {target}" if target else ""))
        lines.append("")
        lines.append(f"Потребность: {issue.get('summary') or issue.get('title', '')}")
        lines.append(f"Запросили: {people} {_plural(people, 'участник', 'участника', 'участников')} фокус-группы.")
        if issue.get("expected"):
            lines.append(f"Предложенное решение: {issue['expected']}")
        lines.append("Критерий готовности: сценарий из потребности выполняется в приложении, существующие сценарии не нарушены.")
        missing = [] if issue.get("summary") else ["описание потребности"]

    rel = related_lines(repo, issue)
    if rel:
        lines.append("")
        lines.append("Связанные: " + "; ".join(rel) + ".")
    if issue.get("comment"):
        lines.append("")
        lines.append(f"Комментарий администратора: {issue['comment']}")
    has_screens = any(
        m.get("issue_id") == issue["issue_id"] and '"status":"ok"' in (m.get("screens") or "")
        for m in repo.rows("Messages")
    )
    if has_screens:
        lines.append(f"Скриншоты: в карточке {issue['issue_id']}, приложить вручную.")
    if missing:
        lines.append("")
        lines.append("Не хватает данных: " + ", ".join(missing) + " — уточнить у автора и QA.")
    return "\n".join(lines).strip() + "\n"
