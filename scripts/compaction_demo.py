"""End-to-end demo: trigger compaction and show the console + audit output.

Drives the in-process ADK runner with a scripted LLM and a stub
summarizer, configured with a deliberately low token threshold so
compaction fires after a few turns. Prints the captured stderr (DEBUG
logs from our wrapper) and audit.jsonl contents — i.e. exactly what an
operator would see in a real session with the same env vars set.

Run: `.venv/bin/python scripts/compaction_demo.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    # Run the actual demo in a subprocess with a fresh env so the
    # configure_logging() call at agent import sees our env vars.
    audit_path = Path(tempfile.mkdtemp(prefix="compaction_demo_")) / "audit.jsonl"
    env = {
        **os.environ,
        "ADK_CC_API_KEY": "sk-dummy-for-demo",
        # Deliberately tiny threshold so a 3-turn session crosses it.
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "200",
        "ADK_CC_COMPACTION_EVENT_RETENTION": "2",
        # No ADK_CC_COMPACTION_MODEL set — proves the always-install fix.
        "ADK_CC_LOG_LEVEL": "DEBUG",
        "ADK_CC_AUDIT_LOG": str(audit_path),
    }
    # Strip env vars that would change the test surface.
    for k in (
        "ADK_CC_COMPACTION_INTERVAL",
        "ADK_CC_COMPACTION_OVERLAP",
        "ADK_CC_COMPACTION_MODEL",
        "ADK_CC_COMPACTION_TIMEOUT_S",
    ):
        env.pop(k, None)

    print("=" * 70)
    print("env:")
    print(f"  ADK_CC_COMPACTION_TOKEN_THRESHOLD = {env['ADK_CC_COMPACTION_TOKEN_THRESHOLD']}")
    print(f"  ADK_CC_COMPACTION_EVENT_RETENTION = {env['ADK_CC_COMPACTION_EVENT_RETENTION']}")
    print(f"  ADK_CC_COMPACTION_MODEL           = <unset, falls back to main model>")
    print(f"  ADK_CC_LOG_LEVEL                  = {env['ADK_CC_LOG_LEVEL']}")
    print(f"  ADK_CC_AUDIT_LOG                  = {env['ADK_CC_AUDIT_LOG']}")
    print("=" * 70)

    # Run the inner script that actually drives a Runner.
    here = Path(__file__).parent
    inner = here / "_compaction_demo_inner.py"
    result = subprocess.run(
        [sys.executable, str(inner)],
        env=env,
        capture_output=True,
        text=True,
    )

    print("\n--- STDERR (logs filtered to compaction + warnings) ---")
    # Drop the noisy authlib / experimental-feature warnings so the
    # signal is visible. Keep everything else.
    for line in result.stderr.splitlines():
        if "authlib" in line or "FeatureName" in line or "check_feature_enabled" in line:
            continue
        print(line)

    print("\n--- STDOUT (script narration) ---")
    print(result.stdout)

    print("\n--- AUDIT JSONL (only compaction_* events) ---")
    if audit_path.exists():
        for line in audit_path.read_text().splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("event", "").startswith("compaction"):
                print(json.dumps(evt, indent=2))
    else:
        print(f"(no audit log written at {audit_path})")

    print("\nexit code:", result.returncode)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
