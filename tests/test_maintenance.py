from app.maintenance import purge_chat
from app.pipeline.analyzer import Analyzer
from app.services import link_issues, merge_issues

from .helpers import ScriptedClassifier, add_msg, make_repo
from .test_pipeline import BUG


async def test_purge_chat_removes_its_messages_and_issues_only():
    repo = await make_repo()
    repo.insert("Chats", {"chat_id": "-1003", "title": "тест", "active": "1"})
    add_msg(repo, "Тест: не грузятся карты", chat="-1003", author="u1", minutes_ago=50)       # FP-001
    add_msg(repo, "Тест: ошибка пароля", chat="-1003", author="u2", minutes_ago=48)           # FP-002
    add_msg(repo, "Реальный баг перевода", chat="-1001", author="u3", minutes_ago=46)         # FP-003
    add_msg(repo, "И у меня перевод", chat="-1003", author="u4", minutes_ago=44)              # сигнал к FP-003
    clf = ScriptedClassifier(BUG, BUG, BUG, {**BUG, "match_issue": "FP-003", "relation": "same"})
    await Analyzer(repo, clf, pause_min=10).run("t", force=True)
    merge_issues(repo, "FP-002", "FP-001", "Админ")          # объединение внутри тестового чата
    link_issues(repo, "FP-003", "FP-001", "Похожее — проверить", "AI")
    await repo.commit()
    assert repo.get("Issues", "FP-003")["participant_count"] == "2"

    res = await purge_chat(repo, "-1003")

    assert res["issues_removed"] == ["FP-001", "FP-002"] and res["issues_recounted"] == ["FP-003"]
    assert {m["chat_id"] for m in repo.rows("Messages")} == {"-1001"}
    assert {i["issue_id"] for i in repo.rows("Issues")} == {"FP-003"}
    assert not repo.rows("Links")
    assert not [a for a in repo.rows("Audit") if a["object_id"] in ("FP-001", "FP-002")]
    assert repo.get("Issues", "FP-003")["participant_count"] == "1"
    assert repo.get("Chats", "-1003")["active"] == "1"
    # в самом листе тоже удалено
    assert all(r[0] != "FP-001" for r in repo.backend.sheets["Issues"][1:])
    assert repo.next_issue_id() == "FP-004"
