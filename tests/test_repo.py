import pytest

from app.storage.backends import MemoryBackend
from app.storage.repo import Repo


@pytest.fixture
async def repo():
    r = Repo(MemoryBackend())
    await r.init()
    return r


async def test_init_creates_tabs_and_settings(repo):
    sheets = repo.backend.sheets
    assert "Issues" in sheets and sheets["Issues"][0][0] == "issue_id"
    assert repo.setting("auto_analysis") == "1"
    assert len(sheets["Settings"]) == 1 + 4


async def test_insert_update_commit_roundtrip(repo):
    repo.insert("Issues", {"issue_id": "FP-001", "title": "=HYPERLINK(\"x\")", "status": "Новое"})
    await repo.commit()
    old = repo.update("Issues", "FP-001", {"status": "Принято", "title": "=HYPERLINK(\"x\")"})
    assert old == {"status": "Новое"}
    await repo.commit()
    fresh = Repo(repo.backend)
    await fresh.load()
    rec = fresh.get("Issues", "FP-001")
    assert rec["status"] == "Принято"
    assert rec["title"].startswith("=")  # хранится как текст
    assert repo.next_issue_id() == "FP-002"


async def test_update_finds_row_after_manual_sort(repo):
    for n in range(1, 4):
        repo.insert("Issues", {"issue_id": f"FP-00{n}", "title": f"t{n}"})
    await repo.commit()
    sheet = repo.backend.sheets["Issues"]
    sheet[1], sheet[3] = sheet[3], sheet[1]  # кто-то отсортировал строки вручную
    repo.update("Issues", "FP-001", {"title": "changed"})
    await repo.commit()
    titles = {row[0]: row[repo.headers["Issues"].index("title")] for row in sheet[1:]}
    assert titles == {"FP-001": "changed", "FP-002": "t2", "FP-003": "t3"}


async def test_failed_commit_keeps_changes(repo):
    repo.insert("Messages", {"msg_id": "1:1", "state": "В очереди"})
    repo.backend.fail_next = 1
    with pytest.raises(ConnectionError):
        await repo.commit()
    assert repo.pending_changes == 1
    await repo.commit()
    assert repo.pending_changes == 0
    assert len(repo.backend.sheets["Messages"]) == 2


async def test_partial_failure_does_not_duplicate(repo):
    repo.insert("Messages", {"msg_id": "1:1"})
    repo.insert("Issues", {"issue_id": "FP-001"})
    repo.backend.fail_next = 0

    calls = {"n": 0}
    original = repo.backend.append

    def flaky(tab, rows):
        calls["n"] += 1
        if tab == "Issues" and calls["n"] == 2:
            raise ConnectionError("boom")
        original(tab, rows)

    repo.backend.append = flaky
    with pytest.raises(ConnectionError):
        await repo.commit()
    await repo.commit()
    assert len(repo.backend.sheets["Messages"]) == 2
    assert len(repo.backend.sheets["Issues"]) == 2


async def test_deleted_row_is_appended_again(repo):
    repo.insert("Chats", {"chat_id": "-100", "title": "A", "active": "1"})
    await repo.commit()
    del repo.backend.sheets["Chats"][1]
    repo.update("Chats", "-100", {"title": "B"})
    await repo.commit()
    assert repo.backend.sheets["Chats"][1][1] == "B"


async def test_extra_user_columns_preserved(repo):
    repo.backend.sheets["Chats"][0].append("заметка")
    repo.backend.sheets["Chats"].append(["-1", "Чат", "", "1", "", "", "важный"])
    await repo.load()
    repo.update("Chats", "-1", {"title": "Чат 2"})
    await repo.commit()
    assert repo.backend.sheets["Chats"][1][-1] == "важный"
