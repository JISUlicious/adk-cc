"""End-to-end demo: drop a CLAUDE.md, run a turn, show that the
content lands in the model's system_instruction.

Drives the in-process Runner with a scripted LLM so no model server
is needed. Captures stderr (DEBUG / INFO logs from
ProjectContextPlugin) and the audit JSONL (`project_context_loaded`
+ `model_request` showing the prepended block).

Run: `.venv/bin/python scripts/project_context_demo.py`
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    project_dir = Path(tempfile.mkdtemp(prefix="adk_cc_ctx_demo_"))
    audit_path = project_dir / "audit.jsonl"
    claude_md = project_dir / "CLAUDE.md"
    claude_md.write_text(
        "# Project conventions\n\n"
        "- Use uv, not pip.\n"
        "- Tests live in `tests/`.\n"
        "- Run `make lint` before commits.\n"
    )

    env = {
        **os.environ,
        "ADK_CC_API_KEY": "sk-dummy-for-demo",
        "ADK_CC_LOG_LEVEL": "DEBUG",
        "ADK_CC_AUDIT_LOG": str(audit_path),
        # Turn on raw-model-IO trace too, so the demo can show the
        # prepended block landing in the actual LlmRequest the model
        # would see.
        "ADK_CC_LOG_MODEL_IO": "1",
    }
    for k in (
        "ADK_CC_DISABLE_PROJECT_CONTEXT",
        "ADK_CC_CONTEXT_FILES",
        "ADK_CC_CONTEXT_FILES_MAX_BYTES",  # live name
        "ADK_CC_CONTEXT_MAX_BYTES",        # deprecated old name
    ):
        env.pop(k, None)

    print("=" * 70)
    print(f"project dir: {project_dir}")
    print(f"  CLAUDE.md   : {claude_md}")
    print(f"  audit.jsonl : {audit_path}")
    print(f"env:")
    print(f"  ADK_CC_LOG_LEVEL    = DEBUG")
    print(f"  ADK_CC_AUDIT_LOG    = <audit.jsonl>")
    print(f"  ADK_CC_LOG_MODEL_IO = 1 (raw model IO trace)")
    print("=" * 70)

    inner = Path(__file__).parent / "_project_context_demo_inner.py"
    result = subprocess.run(
        [sys.executable, str(inner)],
        env={**env, "ADK_CC_CTX_DEMO_CWD": str(project_dir)},
        capture_output=True,
        text=True,
        cwd=project_dir,
    )

    print("\n--- STDOUT (script narration) ---")
    print(result.stdout)

    print("--- STDERR (filtered to project_context + warnings) ---")
    for line in result.stderr.splitlines():
        if "authlib" in line or "FeatureName" in line or "check_feature_enabled" in line:
            continue
        if (
            "project_context" in line
            or "ProjectContextPlugin" in line
            or "WARNING" in line
            or "ERROR" in line
            or "Traceback" in line
        ):
            print(line)

    print("\n--- AUDIT JSONL (filtered to project_context + model_request) ---")
    if audit_path.exists():
        for line in audit_path.read_text().splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev_type = evt.get("event", "")
            if ev_type == "project_context_loaded":
                print(json.dumps(evt, indent=2, default=str))
            elif ev_type == "model_request":
                # The raw payload includes our prepended block in the
                # system_instruction. Pull it out and print just the
                # relevant slice so the demo doesn't dump the whole
                # tool list.
                try:
                    payload = json.loads(evt.get("payload", "{}"))
                    si = payload.get("config", {}).get("system_instruction")
                    if isinstance(si, str) and "adk-cc:context" in si:
                        idx = si.find("adk-cc:context")
                        # Show ~400 chars around the context marker.
                        start = max(0, idx - 50)
                        end = min(len(si), idx + 350)
                        print("\nmodel_request system_instruction contains the prepended block:")
                        print(f"  ... {si[start:end]} ...")
                        break
                except Exception:
                    pass
    else:
        print(f"(no audit log at {audit_path})")

    print("\nexit code:", result.returncode)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
