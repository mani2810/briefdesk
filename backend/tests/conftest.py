"""Test setup: an isolated database, the scripted offline LLM and offline search - no network, no key."""
import os
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="briefdesk-tests-"))
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": f"sqlite:///{(_tmp / 'test.db').as_posix()}",
    "CHECKPOINT_DB": str(_tmp / "checkpoints.db"),
    "DATA_DIR": str(_tmp / "data"),
    "LLM_MODE": "fake",
    "SEARCH_PROVIDER": "fake",
    "MCP_CLIENT_ENABLED": "false",
    "BCRYPT_ROUNDS": "4",
    "LOG_LEVEL": "WARNING",
    "GROQ_API_KEY": "",
    "MESSAGES_PER_MINUTE": "1000",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.ratelimit import limiter  # noqa: E402
from app.main import app  # noqa: E402

limiter.enabled = False          # test_security re-enables it where limits are the point

ADMIN = ("admin@briefdesk.dev", "ChangeMe!2026")


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


_counter = [0]


def register(client, name="Tester"):
    _counter[0] += 1
    email = f"user{_counter[0]}_{int(time.time() * 1000) % 100000}@example.com"
    r = client.post("/api/v1/auth/register", json={"email": email, "name": name, "password": "Password123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, email


def login(client, email, password):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def user(client):
    headers, _ = register(client)
    return headers


@pytest.fixture
def admin(client):
    return login(client, *ADMIN)


def wait_for(client, headers, brief_id, statuses, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        b = client.get(f"/api/v1/briefs/{brief_id}", headers=headers).json()
        if b["status"] in statuses:
            return b
        time.sleep(0.1)
    raise AssertionError(f"brief {brief_id} stuck in {b['status']}, wanted {statuses}")


def events(client, headers, brief_id, kind=None):
    evs = client.get(f"/api/v1/briefs/{brief_id}/events", headers=headers).json()
    return [e for e in evs if kind is None or e["kind"] == kind]


def wait_event(client, headers, brief_id, kind, count=1, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = events(client, headers, brief_id, kind)
        if len(found) >= count:
            return found
        time.sleep(0.1)
    kinds = [e["kind"] for e in events(client, headers, brief_id)]
    raise AssertionError(f"no {kind} event on brief {brief_id}: {kinds}")
