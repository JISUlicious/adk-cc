"""Route test for `/desktop/sessions/backend` — the per-session backend truth.

The badge's contract: once a session has run a turn, the endpoint reports the
RESOLVED backend object (source="live"), which can differ from the global
setting; before that it predicts from config (source="config"). Also pins the
isolation semantics — ssh is remote but NOT isolated; container/docker/… are.

Run: `uv run python tests/test_session_backend_route.py`
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")  # don't inherit the repo .env's backend
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ["ADK_CC_DESKTOP"] = "1"  # routes only mount in desktop mode
# Deterministic config-prediction baseline for the "config" source tests.
os.environ["ADK_CC_SANDBOX_BACKEND"] = "noop"


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.service.desktop_routes import mount_desktop_routes

    app = FastAPI()
    mount_desktop_routes(app)
    return TestClient(app)


def _get(client, session_id: str) -> dict:
    r = client.get("/desktop/sessions/backend", params={"session_id": session_id})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def test_config_source_before_first_turn():
    """Unseeded session → config prediction (noop baseline, not isolated)."""
    body = _get(_client(), "sess-never-ran")
    assert body == {
        "source": "config",
        "backend": "noop",
        "detail": None,
        "isolated": False,
    }, body
    print("OK config_source_before_first_turn")


def test_live_source_reports_resolved_backend():
    """A seeded session reports the ACTUAL backend object — here a fake
    ssh-shaped backend with a host detail — regardless of global config."""
    from adk_cc.sandbox import note_session_backend
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    class FakeSsh(NoopBackend):
        name = "ssh"
        host = "dev@remotebox"

    note_session_backend("sess-ssh-1", FakeSsh())
    body = _get(_client(), "sess-ssh-1")
    assert body == {
        "source": "live",
        "backend": "ssh",
        "detail": "dev@remotebox",
        "isolated": False,  # remote ≠ isolated — the UI copy depends on this
    }, body
    print("OK live_source_reports_resolved_backend")


def test_live_source_container_is_isolated():
    from adk_cc.sandbox import note_session_backend
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    class FakeContainer(NoopBackend):
        name = "container"

    note_session_backend("sess-cont-1", FakeContainer())
    body = _get(_client(), "sess-cont-1")
    assert body["source"] == "live" and body["backend"] == "container"
    assert body["isolated"] is True, body
    print("OK live_source_container_is_isolated")


def test_live_beats_config_divergence():
    """The reason this endpoint exists: global config says noop, but THIS
    session resolved to container (e.g. per-session factory) — live wins."""
    from adk_cc.sandbox import note_session_backend
    from adk_cc.sandbox.backends.noop_backend import NoopBackend

    class FakeContainer(NoopBackend):
        name = "container"

    note_session_backend("sess-diverge-1", FakeContainer())
    client = _client()
    live = _get(client, "sess-diverge-1")
    fresh = _get(client, "sess-diverge-other")
    assert live["backend"] == "container" and live["source"] == "live"
    assert fresh["source"] == "config", fresh
    print("OK live_beats_config_divergence")


def test_missing_session_id_400():
    client = _client()
    r = client.get("/desktop/sessions/backend")
    assert r.status_code == 400, r.status_code
    print("OK missing_session_id_400")


def main():
    test_config_source_before_first_turn()
    test_live_source_reports_resolved_backend()
    test_live_source_container_is_isolated()
    test_live_beats_config_divergence()
    test_missing_session_id_400()
    print("\nall session-backend route tests passed")


if __name__ == "__main__":
    main()
