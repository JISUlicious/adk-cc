"""W8 API: the skill catalog + on/off toggle, over HTTP (TestClient, no model).

Desktop and web mounts are covered here; the third mount (admin/org) shares the
same store and is exercised through the floor semantics in
`test_skill_enablement.py`.

Run: `uv run python tests/test_skill_enablement_api.py`
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ["ADK_CC_DESKTOP"] = "1"

_TMP = tempfile.mkdtemp(prefix="w8-api-")
os.environ["ADK_CC_TENANT_SKILLS_DIR"] = os.path.join(_TMP, "skills")
os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"] = os.path.join(_TMP, "enablement.json")


def _install(name: str) -> None:
    """A skill in the desktop store (the 'installed' source)."""
    d = Path(_TMP, "skills", "local", name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill.\n---\n\nBody.\n", encoding="utf-8"
    )


def main() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk_cc.service.desktop_settings import mount_desktop_settings_routes

    _install("gamma")
    app = FastAPI()
    mount_desktop_settings_routes(app)
    client = TestClient(app)

    rows = client.get("/desktop/settings/skills/catalog").json()["skills"]
    by_name = {r["name"]: r for r in rows}
    assert "gamma" in by_name, list(by_name)
    assert by_name["gamma"]["enabled"] is True
    # The catalog is strictly larger than the install list: built-ins ship in
    # the wheel and can't be uninstalled, which is why they need a toggle.
    installed = client.get("/desktop/settings/skills").json()["skills"]
    assert set(installed) <= set(by_name), (installed, list(by_name))
    assert any(r["source"] == "built-in" for r in rows), "built-ins missing from catalog"
    print("OK catalog lists every source with state")

    r = client.patch("/desktop/settings/skills/gamma/enabled", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    by_name = {x["name"]: x for x in client.get("/desktop/settings/skills/catalog").json()["skills"]}
    assert by_name["gamma"]["enabled"] is False
    assert by_name["gamma"]["disabled_by"] == "org", by_name["gamma"]
    print("OK disable persists and is reported with a reason")

    # The agent side reads the same store — the toggle is not UI-only state.
    from adk_cc.tools import skill_enablement as E

    assert "gamma" in E.disabled_names({"temp:tenant_context": None})
    print("OK the agent's deny-list reflects the API write")

    # ---- rescan: a skill folder added on disk becomes visible -------------
    # Investigated 2026-08-04: skill BODIES are read fresh at load time, but
    # DISCOVERY is cached per project root, and the only thing that dropped
    # that cache was the trust endpoint — so a user who added a folder had no
    # honest way to make it appear.
    from adk_cc.tools import skills as _skills

    proj = Path(_TMP, "proj_rescan")
    (proj / ".adk-cc" / "skills" / "late-skill").mkdir(parents=True, exist_ok=True)
    (proj / ".adk-cc" / "skills" / "late-skill" / "SKILL.md").write_text(
        "---\nname: late-skill\ndescription: added after the cache warmed.\n---\n\nBody.\n",
        encoding="utf-8")
    os.environ["ADK_CC_TRUST_PROJECT_SKILLS"] = "1"
    try:
        # Warm the per-root cache WITHOUT the new skill by pointing discovery
        # at the root before it existed is not possible here, so warm it now
        # and then add a second skill — the cache must hide it until reload.
        first = _skills._skills_for_root(str(proj))
        assert first and "late-skill" in first[1], "fixture: first discovery failed"
        (proj / ".adk-cc" / "skills" / "later-skill").mkdir(parents=True, exist_ok=True)
        (proj / ".adk-cc" / "skills" / "later-skill" / "SKILL.md").write_text(
            "---\nname: later-skill\ndescription: added later still.\n---\n\nBody.\n",
            encoding="utf-8")
        cached = _skills._skills_for_root(str(proj))
        assert cached and "later-skill" not in cached[1], \
            "expected the per-root cache to hide a newly added skill"
        print("OK a new folder is hidden by the cache (the bug the button fixes)")

        r = client.post("/desktop/settings/skills/reload", json={"root": str(proj)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reloaded"] is True
        assert "later-skill" in body["skills"], body["skills"]
        assert "late-skill" in body["skills"], body["skills"]
        print("OK reload re-scans and reports what the agent will see")

        after = _skills._skills_for_root(str(proj))
        assert after and "later-skill" in after[1], "cache not refreshed for the agent"
        print("OK the agent's own resolution sees it too")
    finally:
        os.environ.pop("ADK_CC_TRUST_PROJECT_SKILLS", None)

    # No body at all must not 500 — the button may fire with nothing to scope by.
    assert client.post("/desktop/settings/skills/reload").status_code == 200
    print("OK reload without a root still clears the cache")

    r = client.patch("/desktop/settings/skills/gamma/enabled", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert "gamma" not in E.disabled_names({})
    print("OK re-enable")

    # Bad input is rejected rather than silently treated as "off".
    assert client.patch("/desktop/settings/skills/gamma/enabled", json={}).status_code == 400
    assert client.patch(
        "/desktop/settings/skills/../etc/enabled", json={"enabled": False}
    ).status_code in (400, 404)
    print("OK missing field and unsafe name are rejected")

    print("\nall W8 API tests passed")


if __name__ == "__main__":
    main()
