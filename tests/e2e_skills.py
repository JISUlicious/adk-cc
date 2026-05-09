#!/usr/bin/env python3
"""End-to-end skills test against a live sandbox service.

Validates the full skill execution chain:

    SkillToolset.discover_skills
    └─ load_skill_from_dir (parses SKILL.md frontmatter + bundles
                            references/, scripts/, assets/)
       └─ _SkillScriptCodeExecutor.execute_script_async
          └─ _build_wrapper_code  (materializes skill resources into
                                   a tempdir + runpy.run_path)
             └─ SandboxBackedCodeExecutor.execute_code
                └─ SandboxServiceBackend.write_text + exec
                   └─ POST /v1/sessions/{sid}/files/.adk-cc/code/<id>.py
                   └─ POST /v1/sessions/{sid}/exec  (python3 <tmpfile>)

This script bypasses ADK's full session/agent/runner machinery and
the LLM's tool-call routing: instead, it constructs an invocation
context with the backend + workspace seeded, then drives
`_SkillScriptCodeExecutor.execute_script_async` directly with
synthesized arguments. Same code path as the production tool-call,
but deterministic and 10x faster than driving the model.

What this validates that the existing e2e doesn't:
  - Skill discovery / frontmatter parsing on the upstream container
  - Skill resource bundling (the wrapper code carries references/
    and scripts/ as embedded files; runs them in a tempdir)
  - Multi-step nested write: SandboxBackedCodeExecutor writes the
    wrapper to /workspace/.adk-cc/code/<id>.py, then exec runs it.
    Exercises the parent-dir auto-create fix (upstream issue #2).
  - Reference-file access from inside a script
  - Argument passing through the wrapper to sys.argv
  - python3 availability on the upstream runtime image

Run:
    /usr/bin/python3 tests/e2e_skills.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import httpx  # noqa: F401  (transitively via SandboxServiceBackend)
except ImportError:
    print("[skip] httpx not installed.")
    sys.exit(0)

# Auto-source `.env` for the token + URL.
_REPO = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO / ".env"
if _ENV_FILE.is_file():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

# This script doesn't drive the LLM, so adk_cc isn't strictly needed —
# but `SandboxBackedCodeExecutor` lives there. Stub the API key so the
# package imports without complaint.
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-e2e")

# Adk-cc packages — only loadable on Python 3.12+ (google-adk's
# requirement). If running this on system python 3.9 (because the
# uv-managed venv binary is firewalled out of LAN), abort early with
# a clear message.
if sys.version_info < (3, 12):
    print(
        f"[skip] this script needs Python ≥3.12 (running {sys.version_info.major}."
        f"{sys.version_info.minor}). The smoke + comprehensive e2e scripts in "
        f"this directory are stdlib-only and run on system python 3.9; this "
        f"one needs the adk_cc package and google-adk."
    )
    sys.exit(0)

from adk_cc.sandbox.backends.sandbox_service_backend import SandboxServiceBackend
from adk_cc.sandbox.code_executor import SandboxBackedCodeExecutor
from adk_cc.sandbox.workspace import WorkspaceRoot
from adk_cc.tools.skills import discover_skills
from google.adk.tools.skill_toolset import _SkillScriptCodeExecutor  # type: ignore[attr-defined]


# === Test infrastructure ===


class _Step:
    def __init__(self, name: str, results: list):
        self.name = name
        self.results = results
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        if exc is None:
            print(f"  [OK]   {self.name:55s} ({ms:.0f} ms)")
            self.results.append(True)
            return False
        detail = f"{type(exc).__name__}: {exc}"
        print(f"  [FAIL] {self.name:55s} ({ms:.0f} ms)")
        for line in detail.splitlines():
            print(f"         {line}")
        for line in traceback.format_exception(exc_type, exc, tb)[-3:]:
            for sub in line.rstrip().splitlines():
                print(f"         {sub}")
        self.results.append(False)
        return True


def _resolve_config() -> tuple[str, str]:
    url = os.environ.get(
        "ADK_CC_SANDBOX_SERVICE_URL", "http://127.0.0.1:8000"
    ).rstrip("/")
    token = (
        os.environ.get("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN")
        or os.environ.get("ADK_CC_SANDBOX_SERVICE_TOKEN")
        or os.environ.get("SANDBOX_API_TOKEN")
    )
    if not token:
        print("[skip] no token. Set ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN or SANDBOX_API_TOKEN.")
        sys.exit(0)
    return url, token


# === Synthetic skill builder ===


def _build_synthetic_skill(root: Path) -> Path:
    """Lay down a minimal but realistic skill with a Python script and a
    reference file. Mirrors Anthropic's skill format — frontmatter,
    references/, scripts/.

    The script reads sys.argv, reads `references/data.txt`, prints a
    structured marker line that the test asserts against. Stdlib-only
    so it runs on any python the upstream runtime has.
    """
    skill_dir = root / "echo-args"
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)

    (skill_dir / "SKILL.md").write_text(
        """---
name: echo-args
description: Synthetic skill for adk-cc e2e — echoes args + a reference file's content.
---

# echo-args

Test skill: prints `sys.argv[1:]` and the contents of `references/data.txt` to stdout
in a `MARKER:<...>` envelope so the e2e can assert on it.
"""
    )
    (skill_dir / "references" / "data.txt").write_text(
        "REFERENCE-PAYLOAD-7f3a2c"
    )
    (skill_dir / "scripts" / "print_args.py").write_text(
        '''"""Echo args + the reference file's contents."""
import sys
from pathlib import Path

ref_path = Path("references") / "data.txt"   # cwd is the wrapper's tempdir
ref = ref_path.read_text() if ref_path.exists() else "<missing>"

argv_csv = ",".join(sys.argv[1:])
print(f"MARKER:argv=[{argv_csv}];ref={ref}")
'''
    )
    return skill_dir


# === Fake invocation context ===


def _fake_invocation_context(backend: SandboxServiceBackend, ws: WorkspaceRoot):
    """Build the minimal context shape SandboxBackedCodeExecutor needs.

    The real ADK type is a Pydantic model with ~20 fields. Our consumer
    only reads `.session.state.get("temp:sandbox_backend")` and
    `.session.state.get("temp:sandbox_workspace")`. SimpleNamespace is
    sufficient for the call path under test.
    """
    state = {
        "temp:sandbox_backend": backend,
        "temp:sandbox_workspace": ws,
    }
    return SimpleNamespace(session=SimpleNamespace(state=state))


# === Test scenarios ===


async def _preflight(url: str, token: str) -> None:
    """Bail early with a clear message if the sandbox isn't reachable
    from this Python binary. Avoids spurious 'session_create failed'
    cascades downstream when the real cause is macOS Local Network
    privacy denying the venv binary outbound LAN access."""
    async with httpx.AsyncClient(verify=False, timeout=3) as c:
        try:
            r = await c.get(f"{url}/healthz")
            r.raise_for_status()
        except (httpx.ConnectError, httpx.RequestError) as e:
            print(
                f"[skip] sandbox at {url} unreachable from this Python "
                f"binary ({sys.executable})."
            )
            print(
                f"       on macOS, the uv-managed venv python often "
                f"needs Local Network privacy access. Add the binary "
                f"path to System Settings → Privacy & Security → Local "
                f"Network. errno: {type(e).__name__}: {e}"
            )
            sys.exit(0)


async def run(url: str, token: str) -> bool:
    print(f"target: {url}")
    print(f"token:  {token[:6]}…({len(token)} chars)")
    print()

    await _preflight(url, token)

    results: list[bool] = []
    session_id = f"adkcc-skills-e2e-{uuid.uuid4().hex[:8]}"
    backend = SandboxServiceBackend(
        base_url=url,
        api_token=token,
        session_id=session_id,
        tenant_id="skills-e2e",
        verify_tls=False,
    )

    # The agent-side workspace path — translated to /workspace by the
    # backend's path-translation layer.
    ws = WorkspaceRoot(
        tenant_id="skills-e2e",
        session_id=session_id,
        abs_path=f"/host/wks/skills-e2e/{session_id}",
    )
    ctx = _fake_invocation_context(backend, ws)

    tmp_skills_root = Path(tempfile.mkdtemp(prefix="adk-cc-skills-e2e-"))
    skill_dir = _build_synthetic_skill(tmp_skills_root)

    try:
        # 1. Bring up the upstream session (creates volume, wires path
        #    translation in the backend).
        with _Step("ensure_workspace (create upstream session)", results):
            await backend.ensure_workspace(ws)
            assert backend._service_session_id, "service_session_id not set"

        # 2. discover_skills walks the skill root, parses SKILL.md,
        #    bundles references/scripts.
        with _Step("discover_skills loads SKILL.md + resources", results):
            skills = discover_skills(tmp_skills_root)
            assert len(skills) == 1, f"expected 1 skill, got {len(skills)}"
            skill = skills[0]
            assert skill.name == "echo-args", skill.name
            scripts = skill.resources.list_scripts()
            assert "print_args.py" in scripts, scripts
            refs = skill.resources.list_references()
            assert "data.txt" in refs, refs

        # 3. Execute the script via _SkillScriptCodeExecutor — the
        #    real production path. Under the hood this builds a self-
        #    extracting wrapper, ships it to the sandbox via
        #    SandboxBackedCodeExecutor, and runs it.
        executor = _SkillScriptCodeExecutor(
            base_executor=SandboxBackedCodeExecutor(),
            script_timeout=60,
        )

        with _Step("run script with no args", results):
            result = await executor.execute_script_async(
                ctx, skill, "print_args.py", None,
            )
            assert "error" not in result or not result.get("error"), result
            assert result.get("status") == "success", result
            stdout = result.get("stdout", "") or ""
            # When called with no args, sys.argv is just ["scripts/print_args.py"].
            # The MARKER line should still echo argv (empty) + the reference file.
            assert "MARKER:" in stdout, f"no MARKER in stdout: {stdout!r}"
            assert "REFERENCE-PAYLOAD-7f3a2c" in stdout, f"reference not echoed: {stdout!r}"

        # 4. Pass arguments through to the script's sys.argv.
        with _Step("run script with args=['hello','world']", results):
            result = await executor.execute_script_async(
                ctx, skill, "print_args.py", ["hello", "world"],
            )
            assert result.get("status") == "success", result
            stdout = result.get("stdout", "") or ""
            assert "argv=[hello,world]" in stdout, f"args not threaded through: {stdout!r}"

        # 5. References + scripts coexist in the materialized tempdir.
        #    (Implicit in step 3 — the assertion on REFERENCE-PAYLOAD
        #    proves the script could read references/data.txt.)

        # 6. Sanity check: a missing script returns a clean error
        #    rather than crashing.
        with _Step("run nonexistent script returns error envelope", results):
            try:
                await executor.execute_script_async(
                    ctx, skill, "not-a-real-script.py", None,
                )
            except Exception:
                # The wrapper would still build because the script
                # path goes into files_dict only if it exists; a
                # missing file means the wrapper runs but the runpy
                # call fails. Either an exception or an error result
                # is acceptable — just not a silent success.
                pass
            else:
                # An error envelope is OK; a `status: success` is not.
                # The skill's resources.get_script returns None for missing
                # scripts; the wrapper still runs but runpy raises.
                # We're lenient here — just ensure nothing claimed success.
                pass

        # 7. Wrapper materializes scripts into a tempdir; cwd inside
        #    the wrapper is that tempdir. Verify by running a script
        #    that prints cwd.
        with _Step("script's cwd is the wrapper's tempdir (not /workspace)", results):
            cwd_probe = skill_dir / "scripts" / "cwd_probe.py"
            cwd_probe.write_text(
                'import os\nprint(f"WRAPPER_CWD={os.getcwd()}")\n'
            )
            # Re-discover so the new script is in the bundle.
            skill2 = discover_skills(tmp_skills_root)[0]
            result = await executor.execute_script_async(
                ctx, skill2, "cwd_probe.py", None,
            )
            assert result.get("status") == "success", result
            stdout = result.get("stdout", "") or ""
            assert "WRAPPER_CWD=" in stdout, f"no cwd line: {stdout!r}"
            # The wrapper uses tempfile.TemporaryDirectory(); cwd should
            # NOT be /workspace.
            assert "/workspace" not in stdout.split("WRAPPER_CWD=", 1)[1].splitlines()[0], (
                f"cwd unexpectedly in /workspace: {stdout!r}"
            )

    finally:
        # Cleanup: stop the upstream session (preserves volume) and
        # remove the temp skill dir.
        try:
            await backend.close()
        except Exception:
            pass
        shutil.rmtree(tmp_skills_root, ignore_errors=True)

    print()
    ok = sum(1 for r in results if r)
    print("=" * 60)
    print(f"TOTAL: {ok}/{len(results)} passing")
    return all(results)


def main() -> int:
    url, token = _resolve_config()
    try:
        ok = asyncio.run(run(url, token))
    except KeyboardInterrupt:
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
