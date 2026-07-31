"""How often each skill was offered to the model, and how often it was used.

The Agent Skills spec puts the burden of triggering on the description: the
model matches a request against it and decides. Nothing in the ecosystem tells
an author when theirs is not working — "my skill never fires" is the single most
common complaint, and the usual advice is to guess at rewording.

adk-cc holds both halves already: the catalogue knows what was offered on a
turn, and the skill tools know what was actually loaded. The ratio is the
feedback loop nobody has. A skill offered on forty turns and used on none has a
description problem, and that is a fact rather than a suspicion.

Deliberately small: two counters and a last-used date per skill, in one JSON
file. This is diagnosis for a human, not analytics — nothing here is sent
anywhere, and nothing reads it back into a prompt.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)
_lock = threading.Lock()

# Invocations already counted, so a turn that makes six model requests counts
# its offers ONCE. Bounded: a session is not immortal, but this process may be.
_counted: set[str] = set()
_MAX_COUNTED = 4096


def _store() -> Optional[Path]:
    try:
        from .. import deployment

        return Path(deployment.data_dir()) / "skill-usage.json"
    except Exception:  # noqa: BLE001 — no data dir (bare tests)
        return None


def _read() -> dict:
    path = _store()
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    path = _store()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        _log.debug("skills: could not persist usage counts: %s", e)


def record_offered(names: list[str], invocation_id: Optional[str]) -> None:
    """Count one turn in which these skills were in the catalogue.

    Keyed on the invocation so the count means "turns", not "model requests" —
    a single turn can make many, and a per-request count would read as heavy
    use of a skill nobody touched.
    """
    if not names:
        return
    with _lock:
        if invocation_id:
            if invocation_id in _counted:
                return
            if len(_counted) >= _MAX_COUNTED:
                _counted.clear()
            _counted.add(invocation_id)
        data = _read()
        skills = data.setdefault("skills", {})
        for n in names:
            row = skills.setdefault(n, {"offered": 0, "used": 0})
            row["offered"] = int(row.get("offered", 0)) + 1
        _write(data)


def record_used(name: str, when: Optional[str] = None) -> None:
    """Count one activation. Called on load, not on catalogue inclusion."""
    if not name:
        return
    with _lock:
        data = _read()
        skills = data.setdefault("skills", {})
        row = skills.setdefault(name, {"offered": 0, "used": 0})
        row["used"] = int(row.get("used", 0)) + 1
        if when:
            row["last_used"] = when
        _write(data)


def usage() -> dict[str, dict]:
    """`{skill: {offered, used, last_used?}}`."""
    data = _read().get("skills")
    return {k: dict(v) for k, v in data.items()} if isinstance(data, dict) else {}
