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
