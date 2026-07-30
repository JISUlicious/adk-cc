"""A skill script that imports its siblings must actually run.

`data-analyst` ships an orchestrator (`premodel_audit.py`) that imports three
probe modules, which in turn import `_probe_utils`. Through
`run_skill_script` every one of those imports failed:

    ModuleNotFoundError: No module named 'collinearity_probe'
    ModuleNotFoundError: No module named '_probe_utils'

ADK materialises ALL of a skill's scripts into a temp dir as `scripts/<name>`,
then runs the requested one with `runpy.run_path('scripts/x.py')` — which does
not put `scripts/` on `sys.path`. The siblings were right there and unimportable.

Driven directly rather than through a model: the live runs proved the point once
and then, on the next run, simply did not call the probe. A fix that depends on
the model choosing to exercise it is not verified.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_script_imports.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")
os.environ.setdefault("ADK_CC_SANDBOX_BACKEND", "noop")
os.environ.setdefault("ADK_CC_NOOP_ACK_HOST_EXEC", "1")

_WS = tempfile.mkdtemp(prefix="skillimp-")
_CSV = os.path.join(_WS, "data.csv")


def _write_csv() -> None:
    import random

    rnd = random.Random(3)
    rows = ["a,b,y"]
    for _ in range(120):
        a = rnd.uniform(0, 5)
        rows.append(f"{a:.3f},{rnd.uniform(0, 5):.3f},{2 * a + rnd.gauss(0, .1):.3f}")
    Path(_CSV).write_text("\n".join(rows) + "\n")


class _Ws:
    abs_path = _WS

    def fs_write_config(self):  # noqa: ANN201
        return None

    def fs_read_config(self):  # noqa: ANN201
        return None


class _Session:
    def __init__(self) -> None:
        self.state: dict = {}
        self.id = "s1"
        self.user_id = "u1"
        self.app_name = "adk_cc"


class _Invocation:
    def __init__(self) -> None:
        self.session = _Session()
        # The executor resolves the backend and workspace from SESSION STATE
        # (normally populated by TenancyPlugin), not from get_workspace — its
        # own error message says so.
        from adk_cc.sandbox.backends.noop_backend import NoopBackend
        from adk_cc.sandbox.workspace import WorkspaceRoot

        self.session.state["temp:sandbox_backend"] = NoopBackend()
        self.session.state["temp:sandbox_workspace"] = WorkspaceRoot(
            tenant_id="local", session_id="s1", abs_path=_WS)


class _Ctx:
    """Enough of a ToolContext for the skill tool: ADK hands
    `_invocation_context` to the code executor, which reads
    `invocation_context.session.state` to resolve the sandbox backend."""

    agent_name = "coordinator"

    def __init__(self) -> None:
        self.state: dict = {}
        self._invocation_context = _Invocation()


def main() -> int:
    _write_csv()
    import adk_cc.sandbox as sandbox
    from adk_cc.tools import skills as sk

    sandbox.get_workspace = lambda ctx: _Ws()          # noqa: ARG005
    import adk_cc.sandbox.code_executor as ce

    ce.get_workspace = lambda ctx: _Ws()               # noqa: ARG005

    toolset = sk.make_skill_toolset()
    if toolset is None:
        print("SKIP: no skills discovered."); return 0
    tool = next((t for t in toolset._tools if t.name == "run_skill_script"), None)
    if tool is None:
        print("SKIP: run_skill_script not in the toolset."); return 0

    res = asyncio.run(tool.run_async(
        args={"skill_name": "data-analyst",
              "file_path": "scripts/premodel_audit.py",
              "args": [_CSV, "--target", "y", "--json"]},
        tool_context=_Ctx()))
    out = (res or {}).get("stdout") or ""
    err = (res or {}).get("stderr") or ""

    if "could not be prepared" in err or "AnalysisEnvError" in err:
        print(f"SKIP: analysis env unavailable — {err[:160]}"); return 0
    # Harness plumbing errors first: an AttributeError from the fake context
    # once produced "[PASS] no ModuleNotFoundError" — technically true and
    # completely meaningless.
    if "AttributeError" in err and "_invocation_context" in err or "has no attribute" in err:
        print(f"HARNESS ERROR (not a product failure): {err.strip().splitlines()[-1]}")
        return 1

    # Two DIFFERENT ModuleNotFoundErrors show up here and conflating them hid
    # progress: a sibling module (the sys.path problem) versus a third-party
    # package (the environment-tier problem). Both were real, in that order.
    siblings = ("collinearity_probe", "_probe_utils", "leakage_probe",
                "null_audit_probe")
    if any(f"No module named '{m}'" in err for m in siblings):
        print("  [FAIL] a SIBLING import still fails (sys.path):",
              err.strip().splitlines()[-1])
        return 1
    print("  [PASS] sibling modules import")
    if "ModuleNotFoundError" in err:
        print("  [FAIL] a PACKAGE is missing (env tiers):",
              err.strip().splitlines()[-1])
        return 1
    print("  [PASS] the packages the probe imports are present")

    # It must have actually produced a verdict, not merely imported cleanly.
    if not out.strip():
        print(f"  [FAIL] the probe produced no output; stderr tail: {err[-300:]}")
        return 1
    print(f"  [PASS] the probe produced output ({len(out)} bytes)")
    lowered = out.lower()
    if not any(k in lowered for k in ("vif", "collinear", "leak", "null", "verdict",
                                      "target", "{")):
        print(f"  [FAIL] output does not look like an audit: {out[:200]!r}")
        return 1
    print("  [PASS] the output looks like the audit it claims to be")
    print("\nall skill-script import tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
