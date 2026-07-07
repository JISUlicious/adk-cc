"""Desktop: add a skill from a LOCAL directory (POST /desktop/settings/skills/from-dir).

Ingests a folder (SKILL.md + files) into the skill store, skips junk (.git, …),
requires a manifest, caps size. Model-free / server-free (TestClient).

Run: `.venv/bin/python tests/test_desktop_skill_from_dir.py`
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ["ADK_CC_DESKTOP"] = "1"
_SKILLS = tempfile.mkdtemp(prefix="adk-cc-skills-")
os.environ["ADK_CC_TENANT_SKILLS_DIR"] = _SKILLS


def _make_skill_dir() -> str:
    d = tempfile.mkdtemp(prefix="my-skill-")
    Path(d, "SKILL.md").write_text("---\nname: demo\n---\nA demo skill.\n", encoding="utf-8")
    Path(d, "helper.py").write_text("print('hi')\n", encoding="utf-8")
    (Path(d) / "resources").mkdir()
    Path(d, "resources", "data.txt").write_text("payload", encoding="utf-8")
    # junk that must NOT be copied
    (Path(d) / ".git").mkdir()
    Path(d, ".git", "config").write_text("[core]\n", encoding="utf-8")
    return d


def main() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from adk_cc.service.desktop_settings import mount_desktop_settings_routes

    app = FastAPI()
    mount_desktop_settings_routes(app)
    client = TestClient(app)

    src = _make_skill_dir()

    # Happy path: name defaults to the folder basename.
    r = client.post("/desktop/settings/skills/from-dir", json={"path": src})
    assert r.status_code == 200, r.text
    name = r.json()["skill_name"]
    dst = Path(_SKILLS) / "local" / name
    assert (dst / "SKILL.md").is_file(), "manifest not copied"
    assert (dst / "helper.py").is_file() and (dst / "resources" / "data.txt").is_file(), "files not copied"
    assert not (dst / ".git").exists(), "junk (.git) should be skipped"
    print("OK copies folder + skips junk")

    # Shows up in the list.
    assert name in client.get("/desktop/settings/skills").json()["skills"]
    print("OK listed")

    # Explicit name override.
    r2 = client.post("/desktop/settings/skills/from-dir", json={"path": src, "name": "renamed"})
    assert r2.status_code == 200 and r2.json()["skill_name"] == "renamed"
    assert (Path(_SKILLS) / "local" / "renamed" / "SKILL.md").is_file()
    print("OK name override")

    # Validation.
    assert client.post("/desktop/settings/skills/from-dir", json={"path": "/no/such/dir-xyz"}).status_code == 400
    nomani = tempfile.mkdtemp()
    Path(nomani, "notes.txt").write_text("x")
    assert client.post("/desktop/settings/skills/from-dir", json={"path": nomani}).status_code == 400
    assert client.post("/desktop/settings/skills/from-dir", json={}).status_code == 400
    print("OK validation (missing dir / no manifest / no path)")

    print("\nall skill-from-dir tests passed")


if __name__ == "__main__":
    main()
