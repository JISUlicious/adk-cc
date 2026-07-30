"""The DOM runtime, checked against the bug that motivated it.

A generated social-deduction game wrote the vote outcome to the DOM and then
began the next round in the same tick, which cleared it. Every element existed,
every handler fired, the verifier's own probes passed, and no player would ever
have seen who was voted out. Three improvised verifications missed it.

The fixtures here are that defect and its fix, reduced to the smallest page that
still reproduces it. If `smoke_page.mjs` cannot separate those two pages, it is
not worth shipping.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_web_smoke_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNNER = (REPO / "agents/adk_cc/skills/web-smoke-check/scripts/smoke_page.mjs")
ENTRY = (REPO / "agents/adk_cc/skills/web-smoke-check/scripts/smoke_page.py")

_BUGGY = """<!doctype html><html><body>
<button id="vote">Vote out Ana</button><div id="result"></div>
<script>
function beginRound() { document.getElementById('result').textContent = ''; }
document.getElementById('vote').addEventListener('click', function () {
  document.getElementById('result').textContent = 'Ana was the traitor.';
  beginRound();
});
</script></body></html>
"""

_FIXED = _BUGGY.replace("  beginRound();\n", "")

_CHECK = """export default async ({ click, text }) => {
  await click('#vote');
  if (!text('#result')) throw new Error('vote produced no visible result');
};
"""

_CRASHY = """<!doctype html><html><body>
<button id="vote">Vote</button><div id="result"></div>
<script>
document.getElementById('vote').addEventListener('click', function () {
  document.getElementById('result').textContent = window.missing.value;
});
</script></body></html>
"""


def _runtime_dir() -> str | None:
    """A dir from which `require('jsdom')` resolves, or None to skip.

    Prefers the shared cache the skill documents. Otherwise installs into a temp
    dir — and if that fails (offline), the test skips rather than reporting a
    product failure, which is the mistake a whole afternoon of live runs made.
    """
    shared = Path.home() / ".adk-cc" / "web-runtime"
    if (shared / "node_modules" / "jsdom").is_dir():
        return str(shared)
    if not shutil.which("npm"):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="webruntime-"))
    out = subprocess.run(["npm", "i", "jsdom", "--silent", "--no-audit", "--no-fund"],
                         cwd=tmp, capture_output=True, text=True, timeout=600)
    if out.returncode != 0 or not (tmp / "node_modules" / "jsdom").is_dir():
        return None
    return str(tmp)


def _run(page_html: str, check_js: str, runtime: str) -> tuple[int, dict]:
    d = Path(tempfile.mkdtemp(prefix="smoke-"))
    (d / "page.html").write_text(page_html)
    (d / "check.mjs").write_text(check_js)
    env = dict(os.environ, ADK_CC_WEB_RUNTIME_DIR=runtime)
    out = subprocess.run(
        ["node", str(RUNNER), str(d / "page.html"), str(d / "check.mjs"), "--json"],
        capture_output=True, text=True, env=env, timeout=300)
    try:
        report = json.loads(out.stdout or "{}")
    except ValueError:
        report = {"stdout": out.stdout[-400:], "stderr": out.stderr[-400:]}
    return out.returncode, report


def _entry(page_html: str, check_js: str, runtime: str,
           home: str | None = None) -> tuple[int, str]:
    d = Path(tempfile.mkdtemp(prefix="entry-"))
    (d / "page.html").write_text(page_html)
    (d / "check.mjs").write_text(check_js)
    env = dict(os.environ, ADK_CC_WEB_RUNTIME_DIR=runtime)
    if home:
        env["HOME"] = home
    out = subprocess.run(
        [sys.executable, str(ENTRY), str(d / "page.html"), str(d / "check.mjs")],
        capture_output=True, text=True, timeout=300, env=env)
    return out.returncode, out.stdout + out.stderr


def test_the_python_entrypoint_is_what_run_skill_script_can_launch(runtime: str) -> None:
    """The reason it exists. A live run had the agent load this skill and then
    fail to execute it twice: `run_skill_script` rejects `.mjs` (ADK supports
    .py/.sh/.bash), and its fallback `node scripts/smoke_page.mjs` exited 1
    because a skill's files are not in the workspace — they are served through
    the skill tools, so that relative path does not exist where commands run.

    The entrypoint is a .py that finds the Node runner beside itself."""
    code, out = _entry(_FIXED, _CHECK, runtime)
    assert code == 0, (code, out[-400:])
    assert "tier: jsdom" in out, out[-200:]

    code, out = _entry(_BUGGY, _CHECK, runtime)
    assert code == 1, (code, out[-400:])
    assert "no visible result" in out, out[-300:]
    print("OK the_python_entrypoint_is_what_run_skill_script_can_launch")


def test_the_entrypoint_says_what_to_install_when_there_is_no_runtime() -> None:
    # HOME too, not just the runtime dir: the runner also looks in
    # `$HOME/.adk-cc/web-runtime`, so on a machine where that cache exists this
    # test would find it and "pass" while checking nothing.
    empty = tempfile.mkdtemp(prefix="noruntime-")
    code, out = _entry(_FIXED, _CHECK, empty, home=empty)
    assert code == 2, (code, out[-300:])
    assert "npm i jsdom" in out
    # It must also say the install needs permission — it writes outside the
    # project, so the agent has to ASK rather than treat the failure as final.
    assert "permission" in out.lower(), out[-300:]
    print("OK the_entrypoint_says_what_to_install_when_there_is_no_runtime")


def main() -> int:
    if not shutil.which("node"):
        print("SKIP: node not available."); return 0
    runtime = _runtime_dir()
    if not runtime:
        print("SKIP: no jsdom and could not install one (offline?)."); return 0

    code, report = _run(_BUGGY, _CHECK, runtime)
    assert code == 1, (code, report)
    assert "no visible result" in (report.get("error") or ""), report
    assert report.get("tier") == "jsdom", report
    print("OK the shipped defect is caught")

    code, report = _run(_FIXED, _CHECK, runtime)
    assert code == 0 and report.get("ok") is True, (code, report)
    print("OK the fixed page passes")

    # An uncaught page exception must fail the run even when the check itself
    # does not look for one — the improvised harnesses swallowed these.
    code, report = _run(_CRASHY, _CHECK, runtime)
    assert code == 1, (code, report)
    assert report.get("pageErrors") or report.get("error"), report
    print("OK an uncaught page error fails the run")

    # A missing control is a different bug from a dead control, and the runner
    # must say which.
    code, report = _run(_FIXED, "export default async ({click}) => "
                                "{ await click('#nope'); };", runtime)
    assert code == 1 and "no element matches" in (report.get("error") or ""), report
    print("OK a missing selector is reported as missing, not as broken behaviour")

    # No runtime at all: exit 2 and TELL the caller how to fix it, instead of
    # silently degrading into something that looks like verification.
    empty = tempfile.mkdtemp(prefix="noruntime-")
    d = Path(tempfile.mkdtemp(prefix="smoke-"))
    (d / "page.html").write_text(_FIXED)
    (d / "check.mjs").write_text(_CHECK)
    out = subprocess.run(
        ["node", str(RUNNER), str(d / "page.html"), str(d / "check.mjs")],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ADK_CC_WEB_RUNTIME_DIR": empty, "HOME": empty})
    assert out.returncode == 2, (out.returncode, out.stdout[-300:], out.stderr[-300:])
    assert "npm i jsdom" in out.stderr, out.stderr[-300:]
    assert "NOT verified" in out.stderr, out.stderr[-300:]
    print("OK with no runtime it refuses and says what to install")

    test_the_python_entrypoint_is_what_run_skill_script_can_launch(runtime)
    test_the_entrypoint_says_what_to_install_when_there_is_no_runtime()

    print("\nall web-smoke-check tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
