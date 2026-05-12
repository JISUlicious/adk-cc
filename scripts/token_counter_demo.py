"""End-to-end demo for the unified token counter (PR C).

Drives ContextGuardPlugin through three scenarios and shows the real
stderr output an operator would see:

  1. chars/4 path — request with text content, no usage_metadata.
     Demonstrates the WARN/REJECT log + the DEBUG `shared=X litellm=Y`
     comparison line.
  2. usage_metadata path — session events carry
     `usage_metadata.prompt_token_count`. Plugin picks it up over the
     chars/4 estimate.
  3. Algorithm parity — feeds the same content list to both ADK's
     internal `_estimate_prompt_token_count` and our
     `estimate_prompt_tokens`. Shows the per-content char counts
     match byte-for-byte (the unification's whole point).

Run: `.venv/bin/python scripts/token_counter_demo.py`
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    env = {
        **os.environ,
        "ADK_CC_API_KEY": "sk-dummy-for-demo",
        "ADK_CC_MAX_CONTEXT_TOKENS": "1000",  # tiny — easy to cross
        "ADK_CC_LOG_LEVEL": "DEBUG",          # surface the comparison log
    }
    for k in (
        "ADK_CC_CONTEXT_WARN_TOKENS",
        "ADK_CC_CONTEXT_REJECT_TOKENS",
        "ADK_CC_AUDIT_LOG",
    ):
        env.pop(k, None)

    print("=" * 70)
    print("env:")
    print(f"  ADK_CC_MAX_CONTEXT_TOKENS = {env['ADK_CC_MAX_CONTEXT_TOKENS']}")
    print(f"  (defaults: WARN=750 = 75%, REJECT=950 = 95%)")
    print(f"  ADK_CC_LOG_LEVEL          = {env['ADK_CC_LOG_LEVEL']}")
    print("=" * 70)

    inner = Path(__file__).parent / "_token_counter_demo_inner.py"
    result = subprocess.run(
        [sys.executable, str(inner)],
        env=env,
        capture_output=True,
        text=True,
    )

    print("\n--- STDOUT (script narration) ---")
    print(result.stdout)

    print("--- STDERR (filtered to ContextGuardPlugin + token_counter) ---")
    for line in result.stderr.splitlines():
        if "authlib" in line or "FeatureName" in line or "check_feature_enabled" in line:
            continue
        # Only keep lines from the plugin / our token_counter module so
        # the demo signal is visible.
        if (
            "ContextGuardPlugin" in line
            or "adk_cc.plugins.context_guard" in line
            or "adk_cc.permissions.token_counter" in line
            or "WARNING" in line
            or "ERROR" in line
            or "Traceback" in line
            or "Error" in line
        ):
            print(line)

    print("\nexit code:", result.returncode)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
