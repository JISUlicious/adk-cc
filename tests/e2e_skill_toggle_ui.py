"""E2E: turn a skill off from the UI and prove the AGENT stops seeing it (W8).

The UI half is the easy half. What makes the toggle real is the second
assertion: after flipping the switch in Settings → Skills, the skill is gone
from BOTH catalogs the agent reads — `list_skills` and the skills XML that
`SkillToolset.process_llm_request` injects into every request's system
instruction — and every skill tool refuses it by name.

No model calls: the agent-side check drives the toolset directly.

Run: .venv/bin/python tests/e2e_skill_toggle_ui.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8934
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _write_skill(root: str, name: str) -> None:
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: The {name} skill, for testing toggles.\n---\n\nBody.\n")


def _agent_sees(skills_dir: str, enablement_file: str, name: str) -> tuple[bool, bool, str]:
    """(in list_skills, in the injected system instruction, tool verdict)."""
    sys.path.insert(0, os.path.join(REPO, "agents"))
    os.environ["ADK_CC_SKILL_ENABLEMENT_FILE"] = enablement_file
    for mod in [m for m in list(sys.modules) if m.startswith("adk_cc.tools.skill")]:
        del sys.modules[mod]
    from pathlib import Path

    from adk_cc.tools.skills import make_skill_toolset

    toolset = make_skill_toolset(skills_dir=Path(skills_dir))

    class _Ctx:
        agent_name = "coordinator"
        state: dict = {}

    class _Req:
        def __init__(self):
            self.instructions: list[str] = []

        def append_instructions(self, items):
            self.instructions.extend(items)

    async def run():
        by_name = {t.name: t for t in toolset._tools}
        xml = await by_name["list_skills"].run_async(args={}, tool_context=_Ctx())
        req = _Req()
        await toolset.process_llm_request(tool_context=_Ctx(), llm_request=req)
        loaded = await by_name["load_skill"].run_async(
            args={"skill_name": name}, tool_context=_Ctx())
        verdict = loaded.get("error_code", "OK") if isinstance(loaded, dict) else "OK"
        return name in xml, name in "\n".join(req.instructions), verdict

    return asyncio.run(run())


def main() -> int:
    dist = os.path.join(REPO, "web", "dist-desktop")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        dist = os.path.join(REPO, "web", "dist")
    if not os.path.isfile(os.path.join(dist, "index.html")):
        print("SKIP: web UI not built."); return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright unavailable."); return 0

    data = tempfile.mkdtemp(prefix="w8-ui-")
    skills_root = os.path.join(data, "skills", "local")   # desktop store layout
    os.makedirs(skills_root, exist_ok=True)
    _write_skill(skills_root, "toggle-me")
    _write_skill(skills_root, "keep-me")
    enablement = os.path.join(data, "skill-enablement.json")

    env = dict(os.environ)
    env.update({
        "ADK_CC_AGENTS_DIR": os.path.join(REPO, "agents"),
        "ADK_CC_ALLOW_NO_AUTH": "1",
        "ADK_CC_DESKTOP": "1",
        "ADK_CC_DESKTOP_DATA": data,
        "ADK_CC_TENANCY_MODE": "single",
        "ADK_CC_GLOBAL_TENANT_ID": "local",
        "ADK_CC_SERVE_UI": "1",
        "ADK_CC_UI_DIST": dist,
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(data, "skills"),
        "ADK_CC_SKILL_ENABLEMENT_FILE": enablement,
        "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            page.click('button[title="Settings"]')
            page.get_by_role("button", name="Skills").first.click()
            page.wait_for_selector('[data-skill="toggle-me"]', timeout=10000)

            row = page.locator('[data-skill="toggle-me"]').first
            builtin_rows = page.locator('[data-skill]')
            check("catalog lists installed AND built-in skills with switches",
                  builtin_rows.count() > 2, builtin_rows.count())
            box = row.locator('input[type="checkbox"]')
            check("a skill starts enabled", box.is_checked())

            box.uncheck()
            page.wait_for_timeout(600)   # optimistic flip + server round-trip
            page.reload(wait_until="networkidle")
            page.click('button[title="Settings"]')
            page.get_by_role("button", name="Skills").first.click()
            page.wait_for_selector('[data-skill="toggle-me"]', timeout=10000)
            after = page.locator('[data-skill="toggle-me"]').first.locator('input[type="checkbox"]')
            check("the switch is still off after a reload (persisted)", not after.is_checked())
            check("the untouched skill is still on",
                  page.locator('[data-skill="keep-me"]').first
                      .locator('input[type="checkbox"]').is_checked())

            browser.close()

        # The point of the feature: the agent's own view changed.
        in_list, in_prompt, verdict = _agent_sees(skills_root, enablement, "toggle-me")
        check("disabled skill is gone from list_skills", not in_list)
        check("disabled skill is gone from the injected system instruction", not in_prompt)
        check("load_skill refuses it by name", verdict == "SKILL_DISABLED", verdict)
        keep_list, keep_prompt, keep_verdict = _agent_sees(skills_root, enablement, "keep-me")
        check("the enabled skill is untouched",
              keep_list and keep_prompt and keep_verdict == "OK", keep_verdict)

        # Re-enable over the API and confirm the agent sees it again.
        r = requests.patch(f"{BASE}/desktop/settings/skills/toggle-me/enabled",
                           json={"enabled": True}, timeout=10)
        check("PATCH re-enables", r.ok and r.json().get("enabled") is True, r.text[:120])
        back_list, back_prompt, back_verdict = _agent_sees(skills_root, enablement, "toggle-me")
        check("re-enabled skill returns to both catalogs and loads",
              back_list and back_prompt and back_verdict == "OK", back_verdict)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
