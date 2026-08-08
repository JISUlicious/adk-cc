"""Runtime-spelled paths must reach the file tools' host validation. (P1)

The workspace hint deliberately teaches the model the RUNTIME's paths —
`container_cwd()`'s docstring: "the workspace hint surfaces THIS to the model
— not the host path". Under Docker that is `/workspace/…`, and tracebacks name
it too. The model then hands those paths to agent-side tools, which validate
HOST allow-lists. Observed in production:

    read_file: read denied by fs_read: /workspace/.adk-cc/skill-runtime/...

for a file that was INSIDE the workspace, readable under its host spelling.

The fix is one crossing: `SandboxBackend.to_host_path` (derived from
`container_cwd`, so the pair cannot drift, and identity for host-exec
backends by construction) applied in `resolve()` BEFORE expanduser/realpath —
a runtime path must never be realpath'd against the host filesystem.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_runtime_path_crossing.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    from adk_cc.sandbox.backends.docker_backend import DockerBackend
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    from adk_cc.sandbox.workspace import WorkspaceRoot
    from adk_cc.tools._fs import resolve

    ws_dir = tempfile.mkdtemp(prefix="crossing-")
    ws = WorkspaceRoot(tenant_id="t", session_id="s", abs_path=ws_dir)
    host = ws.abs_path                     # canonicalised

    # --- the mapping itself, every boundary --------------------------------
    d = DockerBackend(session_id="t", workspace_abs_path=host)
    check("runtime path maps to host",
          d.to_host_path(f"/workspace/.adk-cc/s.py", host)
          == f"{host}/.adk-cc/s.py")
    check("the root itself maps", d.to_host_path("/workspace", host) == host)
    check("prefix is component-bounded (/workspace-evil untouched)",
          d.to_host_path("/workspace-evil/x", host) == "/workspace-evil/x")
    check("paths outside the mapping pass through",
          d.to_host_path("/etc/passwd", host) == "/etc/passwd")
    check("host-exec backends are identity by construction",
          NoopBackend().to_host_path(f"{host}/x.py", host) == f"{host}/x.py")

    # --- through resolve(), which is what the tools call -------------------
    class _Ctx:  # duck-typed ToolContext: state dict is all get_* reads
        def __init__(self, backend, ws):
            self.state = {"temp:sandbox_backend": backend,
                          "temp:sandbox_workspace": ws}

    ctx = _Ctx(d, ws)
    got = resolve("/workspace/.adk-cc/skill-runtime/skl/1/s.py", ctx)
    check("resolve() rewrites the runtime spelling to the host one",
          str(got) == f"{host}/.adk-cc/skill-runtime/skl/1/s.py", str(got))
    # The rewritten path lands under the workspace, so the EXISTING allow
    # judges it — no permission widening.
    check("the mapped path is allowed by the unchanged fs config",
          ws.fs_read_config().allows(str(got)))
    # NB compare against realpath: resolve() has ALWAYS realpath'd local
    # absolutes (macOS /etc -> /private/etc). Only the crossing is new.
    check("an outside path is NOT dragged into the workspace",
          str(resolve("/etc/passwd", ctx)) == os.path.realpath("/etc/passwd"))

    # Identity context: noop backend → resolve behaves exactly as before.
    ctxn = _Ctx(NoopBackend(), ws)
    check("resolve() unchanged for host-exec backends",
          str(resolve(f"{host}/a.txt", ctxn)) == f"{host}/a.txt")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
