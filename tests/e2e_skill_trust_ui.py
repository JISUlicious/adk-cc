"""E2E: the trust gate, and the agent's view before and after.

Two halves, and the second is the one that matters. The UI half shows that a
project's skills are withheld, named, and grantable. The agent half proves the
gate is real: before trusting, the skill is absent from BOTH catalogues the
model reads — `list_skills` and the skills block `process_llm_request` injects
into every request — and `load_skill` refuses it; after trusting, all three
change together.

Also covers R2 in passing: `compatibility` reaches the panel, since that field
is only worth an author's time if someone can see it.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/e2e_skill_trust_ui.py
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
PORT = 8937
BASE = f"http://127.0.0.1:{PORT}"

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + str(detail)) if detail and not ok else ''}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _clone_with_skill(root: str) -> None:
    """A repository that ships a skill — the case the gate exists for."""
    d = os.path.join(root, ".adk-cc", "skills", "repo-shipped")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write("---\nname: repo-shipped\ndescription: Ships inside the "
                "repository being worked on.\ncompatibility: Requires ripgrep "
                "and network access.\n---\n\nBody.\n")


def _agent_sees(project_root: str, name: str) -> tuple[bool, bool, str]:
    """(in list_skills, in the injected system instruction, tool verdict)."""
    sys.path.insert(0, os.path.join(REPO, "agents"))
    for mod in [m for m in list(sys.modules) if m.startswith("adk_cc.tools.skill")]:
        del sys.modules[mod]
    from pathlib import Path

    from adk_cc.tools import skills as sk

    # Exactly how a session resolves its skills: every entry point re-derives
    # the project root from the session's WORKSPACE, so the context has to
    # carry one. Setting the contextvar alone is not enough — the first
    # `process_llm_request` overwrites it with whatever the context says, which
    # is how this test first "proved" a bug that was its own missing session.
    sk.clear_project_skill_cache()
    toolset = sk.make_skill_toolset()

    import adk_cc.sandbox as _sandbox

    class _Ws:
        abs_path = str(project_root)

    _sandbox.get_workspace = lambda ctx: _Ws()      # noqa: ARG005

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
        # Injection FIRST, as in a real turn: `process_llm_request` is what
        # binds this session to its project, and the tools read that binding.
        req = _Req()
        await toolset.process_llm_request(tool_context=_Ctx(), llm_request=req)
        xml = await by_name["list_skills"].run_async(args={}, tool_context=_Ctx())
        loaded = await by_name["load_skill"].run_async(
            args={"skill_name": name}, tool_context=_Ctx())
        verdict = (loaded.get("error_code", "OK")
                   if isinstance(loaded, dict) else "OK")
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

    data = tempfile.mkdtemp(prefix="trust-ui-")
    # The agent-side half runs IN THIS PROCESS, so it must read the same trust
    # store the server writes — otherwise "trusted" is recorded in one data dir
    # and looked up in another, and the gate looks broken when it is not.
    os.environ["ADK_CC_DESKTOP_DATA"] = data
    os.environ["ADK_CC_DESKTOP"] = "1"
    proj = tempfile.mkdtemp(prefix="cloned-repo-")
    subprocess.run(["git", "init", "-q", proj], capture_output=True)
    _clone_with_skill(proj)

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
        "ADK_CC_SANDBOX_BACKEND": "noop",
        # The settings catalogue only mounts when a skill STORE is configured.
        "ADK_CC_TENANT_SKILLS_DIR": os.path.join(data, "skills"),
        "ADK_CC_SKIP_DOTENV": "1",
        "ADK_CC_API_KEY": "stub",
    })
    env.pop("ADK_CC_SKILLS_DIR", None)
    env.pop("ADK_CC_TRUST_PROJECT_SKILLS", None)
    proc = subprocess.Popen(
        [os.path.join(REPO, ".venv/bin/uvicorn"), "adk_cc.service.server:make_app",
         "--factory", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO, env=env,
        stdout=open(os.path.join(data, "server.log"), "w"), stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            try:
                if requests.get(BASE + "/list-apps", timeout=2).ok:
                    break
            except Exception:
                time.sleep(0.25)

        # Bind the project so the desktop shell has one open, exactly as a user
        # would after cloning and opening a folder.
        pid = requests.post(BASE + "/desktop/projects", json={"path": proj},
                            timeout=15).json()["project"]["id"]
        # A turn-less discovery pass, so the server has resolved this project's
        # skill dirs at least once (that is what records the withheld list).
        requests.get(f"{BASE}/desktop/settings/skills/catalog?scope=project"
                     f"&project_id={pid}", timeout=15)

        before = _agent_sees(proj, "repo-shipped")
        check("before trusting, the skill is not in list_skills", not before[0])
        check("nor in the injected system instruction", not before[1])
        check("and load_skill will not load it", before[2] != "OK", before[2])

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector('button[title="Settings"]', timeout=20000)
            page.click('button[title="Settings"]')
            page.get_by_role("button", name="Skills").first.click()
            page.wait_for_timeout(800)

            banner = page.locator(f'[data-untrusted-root="{proj}"]')
            check("the panel says the project's skills are withheld",
                  banner.count() > 0, "(no banner)")
            text = banner.first.inner_text() if banner.count() else ""
            check("it names the skill rather than just counting",
                  "repo-shipped" in text, text[:160])
            check("and says why they are withheld",
                  "repository" in text, text[:160])

            page.screenshot(path=os.path.join(data, "untrusted.png"), full_page=True)
            page.locator(f'[data-trust-root="{proj}"]').first.click()
            page.wait_for_timeout(1200)

            check("after trusting, the banner is gone",
                  page.locator(f'[data-untrusted-root="{proj}"]').count() == 0)
            # R2, on a skill that ships with adk-cc: `compatibility` is the
            # spec's own field for "what this needs", and it only earns an
            # author's time if a user can see it. (A repo-shipped skill is not
            # in this catalogue by design — the panel lists skill STORES, not
            # the open repository.)
            needs = page.locator('[data-skill-needs="web-smoke-check"]')
            check("a skill's declared requirements are shown (compatibility)",
                  needs.count() > 0 and "Node" in (needs.first.inner_text() or ""),
                  needs.first.inner_text() if needs.count() else "(none)")
            page.screenshot(path=os.path.join(data, "trusted.png"), full_page=True)
            browser.close()

        after = _agent_sees(proj, "repo-shipped")
        check("the agent now sees it in list_skills", after[0])
        check("and in the injected system instruction", after[1])
        check("and load_skill loads it", after[2] == "OK", after[2])
        print(f"    screenshots: {data}")
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
