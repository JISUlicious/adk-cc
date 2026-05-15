"""End-to-end test for the bare-agent examples.

Boots `examples/bare_agent.py` and `examples/bare_agent_with_skills.py`
as subprocesses, parses their stdout, and asserts:

  - exit code == 0
  - both demos emit a `project_context_loaded` audit event
  - both demos emit `model_request` + `model_response` audit events
  - `bare_agent.py` shows `tool_count=0` in the model_request event
    (proves the agent runs with zero tools wired)
  - `bare_agent_with_skills.py` shows `tool_count=4` (proves
    SkillToolset dispatch tools made it into the request)

These checks are the regression guard for the plugin
tool-independence invariant: if a future change re-introduces an
eager `adk_cc.tools.*` import inside a plugin, `bare_agent.py` will
fail to boot or fail this test.

Run: `.venv/bin/python tests/test_bare_agent_setup.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BARE = _REPO_ROOT / "examples" / "bare_agent.py"
_SKILLS = _REPO_ROOT / "examples" / "bare_agent_with_skills.py"


def _run(script: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            # Demos read their own env defaults; explicit overrides
            # here keep test runs deterministic.
            "ADK_CC_API_KEY": "sk-dummy-for-tests",
            "ADK_CC_LOG_LEVEL": "INFO",
        },
        cwd=str(_REPO_ROOT),
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _audit_lines(stdout: str) -> list[str]:
    """Pull the printed audit event lines out of the demo stdout."""
    lines: list[str] = []
    in_audit = False
    for line in stdout.splitlines():
        if line.strip() == "--- AUDIT JSONL ---":
            in_audit = True
            continue
        if in_audit:
            if not line.strip():
                continue
            lines.append(line.strip())
    return lines


def test_bare_agent_runs_and_emits_expected_events() -> None:
    rc, stdout, stderr = _run(_BARE)
    assert rc == 0, (
        f"bare_agent.py exited non-zero: rc={rc}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    audit = _audit_lines(stdout)
    joined = "\n".join(audit)
    assert any("project_context_loaded" in ln for ln in audit), (
        f"missing project_context_loaded event\n{joined}"
    )
    assert any("model_request" in ln for ln in audit), (
        f"missing model_request event\n{joined}"
    )
    assert any("model_response" in ln for ln in audit), (
        f"missing model_response event\n{joined}"
    )
    # The whole point of bare_agent.py: zero tools wired.
    req_lines = [ln for ln in audit if "model_request" in ln]
    assert any("tool_count=0" in ln for ln in req_lines), (
        f"expected tool_count=0 in bare model_request, got: {req_lines}"
    )
    print("[bare_agent] passed")


def test_bare_agent_with_skills_runs_and_wires_dispatch_tools() -> None:
    rc, stdout, stderr = _run(_SKILLS)
    assert rc == 0, (
        f"bare_agent_with_skills.py exited non-zero: rc={rc}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    audit = _audit_lines(stdout)
    joined = "\n".join(audit)
    assert any("project_context_loaded" in ln for ln in audit), (
        f"missing project_context_loaded event\n{joined}"
    )
    assert any("model_request" in ln for ln in audit), (
        f"missing model_request event\n{joined}"
    )
    assert any("model_response" in ln for ln in audit), (
        f"missing model_response event\n{joined}"
    )
    # SkillToolset wires four dispatch tools; the request should
    # carry all four.
    req_lines = [ln for ln in audit if "model_request" in ln]
    assert any("tool_count=4" in ln for ln in req_lines), (
        f"expected tool_count=4 in skills-variant model_request, got: {req_lines}"
    )
    # The discovered skill name shows up in the plain stdout header.
    assert "toolset skills:  ['greeter']" in stdout, (
        f"greeter skill missing from toolset summary:\n{stdout}"
    )
    # And the 4 dispatch tools are surfaced by name.
    for dispatch in (
        "list_skills",
        "load_skill",
        "load_skill_resource",
        "run_skill_script",
    ):
        assert dispatch in stdout, (
            f"missing dispatch tool {dispatch!r} in toolset summary:\n{stdout}"
        )
    print("[bare_agent_with_skills] passed")


def main() -> None:
    assert _BARE.exists(), f"missing demo at {_BARE}"
    assert _SKILLS.exists(), f"missing demo at {_SKILLS}"
    test_bare_agent_runs_and_emits_expected_events()
    test_bare_agent_with_skills_runs_and_wires_dispatch_tools()
    print("\nall bare-agent setup tests passed")


if __name__ == "__main__":
    main()
