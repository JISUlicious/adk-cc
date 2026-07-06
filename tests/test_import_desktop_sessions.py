"""Importer: old sqlite session store → per-project JSONL file store.

Builds a throwaway sqlite in ADK's schema, migrates it, and asserts the sessions
(with title, events, and user: state) land in the FileSessionService — and that a
second run is idempotent.

Run: `.venv/bin/python tests/test_import_desktop_sessions.py`
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from google.adk.events.event import Event
from google.genai import types

from adk_cc.service.file_session_service import FileSessionService

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "import_desktop_sessions.py"
)
_spec = importlib.util.spec_from_file_location("import_desktop_sessions", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
migrate = _mod._migrate

APP = "adk_cc"


def _event_data(author: str, text: str, ts: float) -> str:
    ev = Event(author=author, content=types.Content(role=author, parts=[types.Part(text=text)]), timestamp=ts)
    return json.dumps(ev.model_dump(mode="json"))


def _make_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE sessions (app_name TEXT, user_id TEXT, id TEXT, state TEXT, create_time REAL, update_time REAL);
        CREATE TABLE events (id TEXT, app_name TEXT, user_id TEXT, session_id TEXT, invocation_id TEXT, timestamp REAL, event_data TEXT);
        CREATE TABLE app_states (app_name TEXT, state TEXT, update_time REAL);
        CREATE TABLE user_states (app_name TEXT, user_id TEXT, state TEXT, update_time REAL);
        """
    )
    # One titled session for project 'projZ' with two events.
    c.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (APP, "projZ", "s1", json.dumps({"session_title": "My Old Chat"}), 100.0, 101.0),
    )
    c.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
        ("e1", APP, "projZ", "s1", "inv1", 100.0, _event_data("user", "hi", 100.0)),
    )
    c.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?)",
        ("e2", APP, "projZ", "s1", "inv1", 101.0, _event_data("coordinator", "hello there", 101.0)),
    )
    # A user-scoped state (allow rules) to migrate verbatim.
    c.execute(
        "INSERT INTO user_states VALUES (?,?,?,?)",
        (APP, "projZ", json.dumps({"allow_rules": ["rule-a"]}), 101.0),
    )
    c.commit()
    c.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="adk-cc-import-") as tmp:
        db = os.path.join(tmp, "sessions.db")
        base = os.path.join(tmp, "store")
        _make_db(db)

        # Dry run writes nothing.
        s = asyncio.run(migrate(db, base, dry_run=True))
        assert s["migrated"] == 1 and s["events"] == 2, s
        assert not os.path.exists(os.path.join(base, "projects")), "dry run wrote files"
        print("OK dry-run counts + no writes")

        # Real run.
        s = asyncio.run(migrate(db, base, dry_run=False))
        assert s == {"sessions": 1, "migrated": 1, "skipped": 0, "events": 2, "app_states": 0, "user_states": 1}, s

        fss = FileSessionService(base)
        got = asyncio.run(fss.get_session(app_name=APP, user_id="projZ", session_id="s1"))
        assert got is not None, "session did not migrate"
        assert got.state.get("session_title") == "My Old Chat", f"title lost: {got.state}"
        assert got.state.get("user:allow_rules") == ["rule-a"], f"user state lost: {got.state}"
        assert len(got.events) == 2, f"events lost: {len(got.events)}"
        texts = [p.text for e in got.events for p in (e.content.parts or []) if getattr(p, "text", None)]
        assert texts == ["hi", "hello there"], texts
        # It shows in the rail list too.
        lst = asyncio.run(fss.list_sessions(app_name=APP, user_id="projZ"))
        assert any(x.id == "s1" and x.state.get("session_title") == "My Old Chat" for x in lst.sessions)
        print("OK migrated session: title + user-state + events + list")

        # Idempotent: second run skips.
        s2 = asyncio.run(migrate(db, base, dry_run=False))
        assert s2["migrated"] == 0 and s2["skipped"] == 1, s2
        assert len(asyncio.run(fss.get_session(app_name=APP, user_id="projZ", session_id="s1")).events) == 2, "duplicated events"
        print("OK idempotent re-run")

    print("\nall importer tests passed")


if __name__ == "__main__":
    main()
