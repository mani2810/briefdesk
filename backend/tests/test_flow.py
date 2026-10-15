"""End-to-end through the API: topic -> plan -> approval -> agents -> verified report -> chat -> exports."""
from conftest import events, wait_event, wait_for


def _start(client, user, topic="UPI adoption by small merchants in India", **kw):
    r = client.post("/api/v1/briefs", json={"topic": topic, **kw}, headers=user)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_full_research_flow(client, user):
    bid = _start(client, user)
    b = wait_for(client, user, bid, {"awaiting_approval"})
    assert 3 <= len(b["plan"]) <= 6
    assert events(client, user, bid, "plan")[-1]["payload"]["awaiting"] is True

    # approve with an edited plan (the human stays in control)
    plan = b["plan"][:3] + ["Which banks lead merchant acquiring?"]
    r = client.post(f"/api/v1/briefs/{bid}/plan/approve", json={"plan": plan}, headers=user)
    assert r.status_code == 202, r.text
    b = wait_for(client, user, bid, {"done", "failed"})
    assert b["status"] == "done", events(client, user, bid, "error")
    assert b["plan"] == plan
    assert b["version"] == 1 and b["report_md"].startswith("# ")
    assert "## Sources" in b["report_md"]
    assert b["sources"] and all(s["url"].startswith("https://") for s in b["sources"])
    assert b["verified_ratio"] is not None and b["citations"]

    agents = {e["agent"] for e in events(client, user, bid, "trace")}
    assert {"supervisor", "researcher", "writer", "reviewer", "verifier"} <= agents
    routes = [e["payload"]["detail"] for e in events(client, user, bid, "trace") if e["payload"]["event"] == "route"]
    assert any(r.startswith("researcher") for r in routes) and any(r.startswith("FINISH") for r in routes)


def test_chat_question_revision_and_exports(client, user):
    bid = _start(client, user, auto_approve=True)
    wait_for(client, user, bid, {"done"})

    # question -> answered from the report and sources
    before = len(events(client, user, bid, "message"))
    client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "What does the report say about growth?"},
                headers=user)
    msgs = wait_event(client, user, bid, "message", count=before + 2)
    assert msgs[-1]["role"] == "assistant" and msgs[-2]["role"] == "user"

    # export in natural language -> the right file type
    for text, fmt in [("give me this as a PDF", "pdf"), ("can I have a Word doc", "docx"),
                      ("make slides for my manager", "pptx"), ("turn it into an infographic image", "png"),
                      ("export the numbers to Excel", "xlsx")]:
        client.post(f"/api/v1/briefs/{bid}/messages", json={"text": text}, headers=user)
    files = wait_event(client, user, bid, "file", count=5)
    formats = sorted(f["payload"]["file"]["format"] for f in files)
    assert formats == sorted(["pdf", "docx", "pptx", "png", "xlsx"])
    f = files[0]["payload"]["file"]
    r = client.get(f"/api/v1/files/{f['id']}", headers=user)
    assert r.status_code == 200 and len(r.content) == f["size"]
    assert "attachment" in r.headers["content-disposition"]

    # unsupported format -> honest substitution with a note
    client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "make a video of this"}, headers=user)
    files = wait_event(client, user, bid, "file", count=6)
    assert files[-1]["payload"]["file"]["format"] == "pptx"
    assert "can't produce" in files[-1]["payload"]["note"]

    # revision -> v2, v1 kept
    client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "add a section on fraud risks with more data"},
                headers=user)
    wait_event(client, user, bid, "report", count=2, timeout=30)
    b = wait_for(client, user, bid, {"done"})
    assert b["version"] == 2
    versions = client.get(f"/api/v1/briefs/{bid}/versions", headers=user).json()
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["instruction"]


def test_steering_while_running(client, user):
    from app.llm.fake import FakeProvider
    FakeProvider.delay = 0.15                           # slow the brain so we can talk to it mid-run
    try:
        bid = _start(client, user, auto_approve=True)
        wait_for(client, user, bid, {"researching", "writing"})
        r = client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "also cover rural adoption"}, headers=user)
        assert r.json()["intent"] == "steer"
        wait_for(client, user, bid, {"done"}, timeout=60)
        traces = [e["payload"]["detail"] for e in events(client, user, bid, "trace")]
        assert any("Picked up your guidance" in t and "rural" in t for t in traces)
    finally:
        FakeProvider.delay = 0.0


def test_cancel_and_retry(client, user):
    from app.llm.fake import FakeProvider
    FakeProvider.delay = 0.2
    try:
        bid = _start(client, user, auto_approve=True)
        wait_for(client, user, bid, {"researching", "writing", "reviewing"})
        client.post(f"/api/v1/briefs/{bid}/cancel", headers=user)
        b = wait_for(client, user, bid, {"cancelled", "done"})
        assert b["status"] == "cancelled"
    finally:
        FakeProvider.delay = 0.0
    client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "retry"}, headers=user)
    b = wait_for(client, user, bid, {"done"}, timeout=30)
    assert b["report_md"]


def test_isolation_between_users(client, user):
    from conftest import register
    bid = _start(client, user, auto_approve=True)
    wait_for(client, user, bid, {"done"})
    other, _ = register(client, "Other")
    assert client.get(f"/api/v1/briefs/{bid}", headers=other).status_code == 404
    assert client.get(f"/api/v1/briefs/{bid}/events", headers=other).status_code == 404
    assert client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "hi"}, headers=other).status_code == 404
    assert client.post(f"/api/v1/briefs/{bid}/ws-ticket", headers=other).status_code == 404
    client.post(f"/api/v1/briefs/{bid}/messages", json={"text": "as pdf please"}, headers=user)
    f = wait_event(client, user, bid, "file")[0]["payload"]["file"]
    assert client.get(f"/api/v1/files/{f['id']}", headers=other).status_code == 404
    assert all(b["id"] != bid for b in client.get("/api/v1/briefs", headers=other).json())
