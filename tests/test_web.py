from app.config import Config
from app.demo import seed_demo
from app.pipeline.analyzer import Analyzer
from app.pipeline.classifier import HeuristicClassifier
from app.storage.backends import MemoryBackend
from app.storage.repo import Repo
from app.util import add_working_days, today
from app.web.server import AppContext, create_app

H = {"X-Requested-With": "fetch"}


async def make_client(aiohttp_client, view_token=""):
    repo = Repo(MemoryBackend())
    await repo.init()
    seed_demo(repo)
    await repo.commit()
    cfg = Config(demo_mode=True, session_secret="s", view_token=view_token)
    ctx = AppContext(repo=repo, cfg=cfg, analyzer=Analyzer(repo, HeuristicClassifier()),
                     is_admin=lambda uid: uid == -1, classifier_name="heuristic")
    return await aiohttp_client(create_app(ctx)), repo


async def login(client):
    r = await client.get("/auth/demo", allow_redirects=False)
    assert r.status == 302


async def test_viewer_reads_but_cannot_change(aiohttp_client):
    client, repo = await make_client(aiohttp_client)
    r = await client.get("/api/state")
    data = await r.json()
    assert r.status == 200 and not data["me"]["admin"] and len(data["issues"]) == 20
    r = await client.post("/api/issues/FP-204/save", json={"fields": {}, "accept": True}, headers=H)
    assert r.status == 401


async def test_csrf_header_required(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    await login(client)
    r = await client.post("/api/run", json={})
    assert r.status == 403


async def test_accept_bug_requires_due_and_logs_audit(aiohttp_client):
    client, repo = await make_client(aiohttp_client)
    await login(client)
    r = await client.post("/api/issues/FP-204/save", json={"fields": {"due": ""}, "accept": True}, headers=H)
    assert r.status == 422
    assert "срок" in " ".join((await r.json())["errors"]).lower()
    due = add_working_days(today(), 3).isoformat()
    r = await client.post("/api/issues/FP-204/save", json={"fields": {"due": due, "owner": "iOS"}, "accept": True}, headers=H)
    body = await r.json()
    assert r.status == 200, body
    assert body["issue"]["status"] == "Принято" and body["issue"]["accepted_by"].startswith("Демо-администратор")
    assert body["issue"]["due_set_by"].startswith("Демо-администратор")
    fields = {a["field"] for a in repo.rows("Audit") if a["object_id"] == "FP-204"}
    assert {"Статус", "Срок", "Ответственный", "Принял"} <= fields


async def test_resolve_requires_version_result_owner(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    await login(client)
    r = await client.post("/api/issues/FP-202/save", json={"fields": {"status": "Устранено", "owner": "Не назначен"}}, headers=H)
    errs = " ".join((await r.json())["errors"])
    assert r.status == 422 and "версию" in errs and "результат" in errs.lower() and "ответственного" in errs
    r = await client.post("/api/issues/FP-202/save", json={"fields": {
        "status": "Устранено", "owner": "Android", "fix_version": "4.2.1", "resolution": "Исправлено",
        "resolved_at": today().isoformat()}}, headers=H)
    assert r.status == 200


async def test_reject_needs_reason_and_status_per_type(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    await login(client)
    r = await client.post("/api/issues/FP-147/save", json={"fields": {"status": "Отклонено"}}, headers=H)
    assert r.status == 422
    r = await client.post("/api/issues/FP-147/save", json={"fields": {"status": "Принято"}}, headers=H)
    assert r.status == 422  # статус бага у пожелания
    r = await client.post("/api/issues/FP-147/save", json={"fields": {"status": "Отклонено", "reject_reason": "Дубль"}}, headers=H)
    assert r.status == 200


async def test_merge_and_message_actions(aiohttp_client):
    client, repo = await make_client(aiohttp_client)
    await login(client)
    r = await client.post("/api/issues/FP-151/merge", json={"target": "FP-143"}, headers=H)
    body = await r.json()
    assert r.status == 200 and body["issue"]["issue_id"] == "FP-143"
    assert body["issue"]["participant_count"] >= 2
    assert repo.get("Issues", "FP-151")["merged_into"] == "FP-143"
    msg = next(m for m in repo.rows("Messages") if m["state"] == "На проверке")
    r = await client.post(f"/api/messages/{msg['msg_id']}/action", json={"action": "exclude"}, headers=H)
    assert r.status == 422
    r = await client.post(f"/api/messages/{msg['msg_id']}/action", json={"action": "context", "issue_id": "FP-142"}, headers=H)
    assert r.status == 200
    assert repo.get("Messages", msg["msg_id"])["issue_id"] == "FP-142"


async def test_jira_endpoint_and_image(aiohttp_client):
    client, _ = await make_client(aiohttp_client)
    r = await client.get("/api/issues/FP-142/jira")
    text = (await r.json())["text"]
    assert text.startswith("[Баг][iOS и Android] Перевод зависает")
    data = await (await client.get("/api/issues/FP-142")).json()
    shot = data["issue"]["screens"][0]
    r = await client.get(f"/api/image/{shot['msg_id']}/{shot['idx']}")
    assert r.status == 200 and "svg" in r.headers["Content-Type"]
    r = await client.get("/api/image/nope/0")
    assert r.status == 404


async def test_view_token(aiohttp_client):
    client, _ = await make_client(aiohttp_client, view_token="abc")
    assert (await client.get("/api/state")).status == 403
    r = await client.get("/?k=abc", allow_redirects=False)
    assert r.status == 302
    assert (await client.get("/api/state")).status == 200
