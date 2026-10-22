from datetime import timedelta

import pytest

from app.pipeline.analyzer import Analyzer
from app.pipeline.classifier import HeuristicClassifier
from app.pipeline.episodes import build_episodes
from app.pipeline.jira_text import build_jira_text
from app.util import compare_versions, iso, now

from .helpers import ScriptedClassifier, add_msg, make_repo

BUG = {"kind": "issue", "type": "Баг", "section": "Переводы", "operation": "Перевод на карту",
       "priority": "Критичный", "title": "Перевод зависает после подтверждения", "relation": "none",
       "confidence": 0.9, "reason": "Сбой операции", "steps": ["Оформить перевод", "Подтвердить"],
       "actual": "Экран загрузки", "expected": "Экран результата"}


def test_versions():
    assert compare_versions("4.10.0", "4.9.2") == 1
    assert compare_versions("4.2", "4.2.0") == 0
    assert compare_versions("4.1.8", "4.1.9") == -1
    assert compare_versions("", "4.1.9") is None


async def test_episode_grouping_by_author_pause_and_reply():
    repo = await make_repo()
    a = add_msg(repo, "Не проходит перевод", author="u1", minutes_ago=40)
    b = add_msg(repo, "На Android 4.2.0", author="u1", minutes_ago=38)
    c = add_msg(repo, "У меня тоже", author="u2", minutes_ago=36, reply_to=a["message_id"])
    d = add_msg(repo, "Другая тема", author="u1", minutes_ago=15)  # пауза 23 минуты — новый эпизод
    e = add_msg(repo, "Свежий", author="u3", minutes_ago=2)  # ещё не готов
    pending = [a, b, c, d, e]
    ready, waiting = build_episodes(pending, repo.tables["Messages"], 10, now())
    groups = sorted(sorted(m["msg_id"] for m in ep.messages) for ep in ready)
    assert sorted([a["msg_id"], b["msg_id"], c["msg_id"]]) in groups
    assert [d["msg_id"]] in groups
    assert [e["msg_id"]] == [m["msg_id"] for ep in waiting for m in ep.messages]
    ready2, waiting2 = build_episodes(pending, repo.tables["Messages"], 10, now(), force=True)
    assert not waiting2 and len(ready2) == 3


async def test_noise_filtered_without_claude_and_new_issue_created():
    repo = await make_repo()
    add_msg(repo, "Всем доброе утро!", author="u9")
    add_msg(repo, "Подтвердил перевод, деньги списались, а экран всё ещё загружается", author="u1", version="4.2.0", platform="Android")
    clf = ScriptedClassifier(BUG)
    an = Analyzer(repo, clf, pause_min=10)
    res = await an.run("test")
    assert res.filtered == 1 and res.new_issues == 1 and res.claude_calls == 1
    issue = repo.get("Issues", "FP-001")
    assert issue["status"] == "Новое" and issue["platform"] == "Android" and issue["app_version"] == "4.2.0"
    assert issue["msg_count"] == "1" and issue["participant_count"] == "1"
    assert repo.backend.sheets["Issues"][1][0] == "FP-001"  # записано в таблицу
    run = repo.rows("Runs")[-1]
    assert run["new_issues"] == "1"


async def test_signal_to_open_issue_counts_participants_and_platforms():
    repo = await make_repo()
    add_msg(repo, "Перевод завис", author="u1", platform="Android")
    an = Analyzer(repo, ScriptedClassifier(BUG, {**BUG, "match_issue": "FP-001", "relation": "same"}), pause_min=10)
    await an.run()
    add_msg(repo, "И у меня перевод висит", chat="-1002", author="u2", platform="iOS")
    res = await an.run()
    assert res.signals == 1 and res.new_issues == 0
    issue = repo.get("Issues", "FP-001")
    assert issue["participant_count"] == "2"
    assert issue["platform"] == "iOS и Android"
    assert set(issue["chats"].split(",")) == {"-1001", "-1002"}


async def _closed_bug(repo, fix_version="4.1.9"):
    add_msg(repo, "Перевод завис", author="u1", version="4.1.8")
    an = Analyzer(repo, ScriptedClassifier(BUG), pause_min=10)
    await an.run()
    repo.update("Issues", "FP-001", {"status": "Закрыто", "fix_version": fix_version, "resolved_at": iso(),
                                     "resolution": "Исправлено", "owner": "Backend"})
    await repo.commit()


async def test_repeat_on_old_version_is_not_reopened():
    repo = await make_repo()
    await _closed_bug(repo)
    add_msg(repo, "Опять завис перевод", author="u2", version="4.1.8")
    an = Analyzer(repo, ScriptedClassifier({**BUG, "match_issue": "FP-001", "relation": "repeat"}), pause_min=10)
    res = await an.run()
    assert res.new_issues == 0
    msg = [m for m in repo.rows("Messages") if m["text_masked"].startswith("Опять")][0]
    assert msg["issue_id"] == "FP-001" and msg["state_reason"].startswith("Старая версия")
    assert repo.get("Issues", "FP-001")["status"] == "Закрыто"


async def test_repeat_on_new_version_is_regression():
    repo = await make_repo()
    await _closed_bug(repo)
    add_msg(repo, "Опять завис перевод на 4.2.0", author="u2", version="4.2.0")
    an = Analyzer(repo, ScriptedClassifier({**BUG, "priority": "Средний", "match_issue": "FP-001", "relation": "repeat"}), pause_min=10)
    res = await an.run()
    assert res.new_issues == 1
    new = repo.get("Issues", "FP-002")
    assert new["priority"] == "Критичный"  # не ниже исходного
    assert repo.get("Links", "FP-002", "FP-001", "Регрессия")
    assert repo.get("Issues", "FP-001")["status"] == "Закрыто"


async def test_repeat_with_unknown_version_is_similar():
    repo = await make_repo()
    await _closed_bug(repo)
    add_msg(repo, "Опять завис перевод", author="u2")
    an = Analyzer(repo, ScriptedClassifier({**BUG, "match_issue": "FP-001", "relation": "repeat"}), pause_min=10)
    await an.run()
    assert repo.get("Links", "FP-002", "FP-001", "Похожее — проверить")


async def test_ambiguous_goes_to_review_and_context_attaches():
    repo = await make_repo()
    first = add_msg(repo, "Перевод завис", author="u1")
    an = Analyzer(repo, ScriptedClassifier(BUG), pause_min=10)
    await an.run()
    add_msg(repo, "У меня снова не работает", author="u2", minutes_ago=20)
    add_msg(repo, "На Wi-Fi тоже повторяется", chat="-1001", author="u3", minutes_ago=20, reply_to=first["message_id"])
    clf = ScriptedClassifier(
        {"kind": "review", "type": "Баг", "section": "Прочее", "priority": "Средний", "title": "", "relation": "none",
         "confidence": 0.3, "reason": "Не хватает контекста"},
        {"kind": "context", "type": "Баг", "section": "Переводы", "priority": "Средний", "title": "", "relation": "same",
         "confidence": 0.8, "reason": "Уточнение"},
    )
    await Analyzer(repo, clf, pause_min=10).run()
    by_text = {m["text_masked"]: m for m in repo.rows("Messages")}
    assert by_text["У меня снова не работает"]["state"] == "На проверке"
    assert by_text["На Wi-Fi тоже повторяется"]["state"] == "Контекст"
    assert by_text["На Wi-Fi тоже повторяется"]["issue_id"] == "FP-001"
    assert any(inp.reply_issue == "FP-001" for inp in clf.inputs)


async def test_classifier_failure_keeps_messages_queued():
    repo = await make_repo()
    add_msg(repo, "Перевод завис", author="u1")

    class Boom:
        async def classify(self, ep):
            raise RuntimeError("API down")

    res = await Analyzer(repo, Boom(), pause_min=10).run()
    assert res.errors == 1
    assert repo.rows("Messages")[0]["state"] == "В очереди"


async def test_daily_limit_stops_calls():
    repo = await make_repo()
    add_msg(repo, "Перевод завис", author="u1", minutes_ago=50)
    add_msg(repo, "Вклад не открывается", author="u2", minutes_ago=50)
    notes = []

    async def notify(t):
        notes.append(t)

    an = Analyzer(repo, ScriptedClassifier(BUG, BUG), pause_min=10, daily_limit=1, notifier=notify)
    res = await an.run()
    assert res.claude_calls == 1 and res.skipped_limit == 1
    assert any("80%" in n for n in notes)


async def test_orphan_messages_recovered():
    repo = await make_repo()
    m = add_msg(repo, "Перевод завис")
    repo.update("Messages", m["msg_id"], {"state": "Учтено", "issue_id": "FP-077"})
    an = Analyzer(repo, ScriptedClassifier(BUG), pause_min=10)
    res = await an.run()
    assert res.new_issues == 1
    assert repo.get("Messages", m["msg_id"])["issue_id"] == "FP-001"


async def test_new_operation_is_proposed():
    repo = await make_repo()
    add_msg(repo, "Не работает оплата штрафов ГАИ")
    await Analyzer(repo, ScriptedClassifier({**BUG, "section": "Платежи", "operation": "Оплата штрафов", "operation_is_new": True}), pause_min=10).run()
    op = repo.get("Operations", "Платежи", "Оплата штрафов")
    assert op and op["status"] == "Предложена"


async def test_heuristic_classifier_runs_end_to_end():
    repo = await make_repo()
    for sec, op, kw in [("Переводы", "Перевод на карту", "перевод")]:
        repo.insert("Operations", {"section": sec, "operation": op, "keywords": kw, "status": "Утверждена"})
    add_msg(repo, "На Samsung открываю карту HUMO и приложение сразу закрывается", author="u1")
    add_msg(repo, "Хотелось бы скрыть все суммы одним нажатием", author="u2")
    res = await Analyzer(repo, HeuristicClassifier(), pause_min=10).run()
    types = sorted(i["type"] for i in repo.rows("Issues"))
    assert res.new_issues == 2 and types == ["Баг", "Пожелание"]


async def test_jira_text_for_bug():
    repo = await make_repo()
    add_msg(repo, "Перевод завис", author="u1", version="4.2.0", platform="Android")
    await Analyzer(repo, ScriptedClassifier({**BUG, "error_text": "Операция обрабатывается"}), pause_min=10).run()
    issue = repo.get("Issues", "FP-001")
    text = build_jira_text(repo, issue)
    first = text.splitlines()[0]
    assert first == "[Баг][Android] Перевод зависает после подтверждения"
    assert "1 участник · 1 сообщение" in text
    assert "1. Оформить перевод." in text and "«Операция обрабатывается»" in text
    assert "u1" not in text  # без Telegram ID авторов


async def test_profile_learned_from_messages_without_registration():
    repo = await make_repo()
    repo.insert("Participants", {"telegram_id": "u5", "name": "Азиз"})
    add_msg(repo, "На Samsung A54, версия 4.2.0, перевод завис", author="u5", minutes_ago=40)
    await Analyzer(repo, ScriptedClassifier({**BUG, "platform": "Android", "device": "Samsung A54", "app_version": "4.2.0"}), pause_min=10).run()
    p = repo.get("Participants", "u5")
    assert (p["platform"], p["device"], p["app_version"]) == ("Android", "Samsung A54", "4.2.0")
    assert repo.get("Issues", "FP-001")["app_version"] == "4.2.0"
    # следующее сообщение без указания версии получает версию из профиля
    add_msg(repo, "Вклад не открывается", author="u5", minutes_ago=15)
    await Analyzer(repo, ScriptedClassifier({**BUG, "title": "Вклад", "app_version": "4.1.0"}), pause_min=10).run()
    assert repo.get("Participants", "u5")["app_version"] == "4.2.0"  # старая версия не затирает новую
    assert repo.get("Issues", "FP-002")["platform"] == "Android"


async def test_episode_with_bug_and_wish_split_into_two_issues():
    repo = await make_repo()
    add_msg(repo, "После регистрации не подтягиваются карты клиента", author="u1", minutes_ago=30)
    add_msg(repo, "Скрин ниже", author="u1", minutes_ago=29)
    add_msg(repo, "Добавьте кнопку Конвертация на главный экран", author="u1", minutes_ago=25)
    wish = {**BUG, "type": "Пожелание", "priority": "Низкий", "title": "Кнопка Конвертация на главном экране"}
    clf = ScriptedClassifier({**BUG, "parts": [[1, 2], [3]]}, BUG, wish)
    res = await Analyzer(repo, clf, pause_min=10).run("test", force=True)
    assert res.new_issues == 2 and res.claude_calls == 3
    assert clf.inputs[0].allow_split and not clf.inputs[1].allow_split
    assert [len(i.messages) for i in clf.inputs[1:]] == [2, 1]
    assert {i["type"] for i in repo.rows("Issues")} == {"Баг", "Пожелание"}
    assert repo.get("Issues", "FP-001")["msg_count"] == "2"


async def test_split_ignores_single_group():
    repo = await make_repo()
    add_msg(repo, "Перевод завис", author="u1", minutes_ago=30)
    add_msg(repo, "На экране подтверждения", author="u1", minutes_ago=29)
    clf = ScriptedClassifier({**BUG, "parts": [[1, 2]]})
    res = await Analyzer(repo, clf, pause_min=10).run("test", force=True)
    assert res.new_issues == 1 and res.claude_calls == 1


async def test_old_service_messages_cleaned_from_issue():
    repo = await make_repo()
    svc = add_msg(repo, "", author="u1", kind="other", minutes_ago=31)
    add_msg(repo, "Перевод завис", author="u1", minutes_ago=30)
    # как было в старой версии: служебное сообщение попало в обращение
    await Analyzer(repo, ScriptedClassifier(BUG), pause_min=10).run("test", force=True)
    repo.update("Messages", svc["msg_id"], {"state": "Учтено", "issue_id": "FP-001", "state_by": "AI"})
    await Analyzer(repo, ScriptedClassifier(), pause_min=10).run("test")
    m = repo.get("Messages", svc["msg_id"])
    assert m["state"] == "Отфильтровано" and not m["issue_id"]
    assert repo.get("Issues", "FP-001")["msg_count"] == "1"
