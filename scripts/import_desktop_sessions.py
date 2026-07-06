#!/usr/bin/env python3
"""One-time importer: old sqlite session store → the per-project JSONL file store.

Desktop mode switched from a monolithic ``~/.adk-cc-desktop/sessions.db`` (ADK's
DatabaseSessionService) to per-project JSONL files (``FileSessionService``). This
migrates the pre-existing sqlite sessions into the file store so they show up in
the app again. The sqlite DB is left untouched (read-only); re-running is safe —
sessions already present in the file store are skipped.

Usage:
    .venv/bin/python scripts/import_desktop_sessions.py [--dry-run]
        [--db ~/.adk-cc-desktop/sessions.db] [--data ~/.adk-cc-desktop]

Each session's session-scoped state (incl. its generated title) is preserved; all
events are replayed in timestamp order; app:/user: shared state (allow-rules etc.)
is copied from the sqlite app_states/user_states tables verbatim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys

# Make `adk_cc` importable when run from the repo root without install.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents"))

from google.adk.events.event import Event

from adk_cc.service.file_session_service import FileSessionService


async def _migrate(db_path: str, base_dir: str, *, dry_run: bool, registered: set[str] | None = None) -> dict:
    """Migrate sqlite → file store. When `registered` is given, only sessions whose
    user_id (= project id) is in that set are migrated (the rest can't be shown in
    the rail — their project is gone — so migrating them just writes hidden files)."""
    fss = FileSessionService(base_dir)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    stats = {"sessions": 0, "migrated": 0, "skipped": 0, "unregistered": 0, "events": 0, "app_states": 0, "user_states": 0}

    rows = con.execute("SELECT app_name, user_id, id, state FROM sessions").fetchall()
    stats["sessions"] = len(rows)
    for r in rows:
        app, user, sid = r["app_name"], r["user_id"], r["id"]
        if registered is not None and user not in registered:
            stats["unregistered"] += 1
            continue
        if await fss.get_session(app_name=app, user_id=user, session_id=sid) is not None:
            stats["skipped"] += 1
            continue
        state = json.loads(r["state"] or "{}")
        ev_rows = con.execute(
            "SELECT event_data FROM events WHERE app_name=? AND user_id=? AND session_id=? ORDER BY timestamp",
            (app, user, sid),
        ).fetchall()
        if dry_run:
            stats["migrated"] += 1
            stats["events"] += len(ev_rows)
            continue
        session = await fss.create_session(app_name=app, user_id=user, state=state, session_id=sid)
        for er in ev_rows:
            try:
                ev = Event.model_validate(json.loads(er["event_data"]))
            except Exception as e:  # noqa: BLE001 — skip a corrupt row, keep the rest
                print(f"  ! skipping unparsable event in {sid}: {e}")
                continue
            await fss.append_event(session, ev)
            stats["events"] += 1
        stats["migrated"] += 1

    # Shared state: copy the authoritative current app:/user: state verbatim.
    for r in con.execute("SELECT app_name, state FROM app_states").fetchall():
        st = json.loads(r["state"] or "{}")
        if st:
            stats["app_states"] += 1
            if not dry_run:
                fss._merge_app_state(st)
    for r in con.execute("SELECT app_name, user_id, state FROM user_states").fetchall():
        st = json.loads(r["state"] or "{}")
        if st:
            stats["user_states"] += 1
            if not dry_run:
                fss._merge_user_state(r["user_id"], st)

    con.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Import old sqlite sessions into the file store.")
    default_data = os.environ.get("ADK_CC_DESKTOP_DATA") or os.path.expanduser("~/.adk-cc-desktop")
    ap.add_argument("--db", default=os.path.join(default_data, "sessions.db"))
    ap.add_argument("--data", default=default_data, help="file-store base dir")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--only-registered",
        action="store_true",
        help="only migrate sessions whose project is still in projects.json (skip orphans)",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"No sqlite DB at {args.db} — nothing to import.")
        return 0

    registered = None
    if args.only_registered:
        proj_file = os.path.join(args.data, "projects.json")
        try:
            registered = {p.get("id") for p in json.load(open(proj_file))}
        except (OSError, json.JSONDecodeError):
            registered = set()
        print(f"only-registered: {len(registered)} project(s) in {proj_file}")

    print(f"{'DRY RUN — ' if args.dry_run else ''}importing {args.db} → {args.data}")
    stats = asyncio.run(_migrate(args.db, args.data, dry_run=args.dry_run, registered=registered))
    print(
        f"sessions: {stats['sessions']} total, {stats['migrated']} "
        f"{'to migrate' if args.dry_run else 'migrated'}, {stats['skipped']} already present, "
        f"{stats['unregistered']} skipped (project gone)\n"
        f"events: {stats['events']}   app_states: {stats['app_states']}   user_states: {stats['user_states']}"
    )
    if args.dry_run:
        print("(dry run — no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
