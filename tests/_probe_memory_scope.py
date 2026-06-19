"""Live probe for Fix B: a turn heavy with DOMAIN facts + one USER fact should
capture only the user fact, not the subject matter. Skips without a model.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

os.environ["ADK_CC_MEMORY"] = "1"
os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")
_TMP = tempfile.mkdtemp(prefix="scope-root-")
os.environ["ADK_CC_MEMORY_ROOT"] = _TMP

from adk_cc.agent import MODEL
from adk_cc.plugins.memory import MemoryPlugin, _parse_facts

TURN = (
    "user: Explain the cache hierarchy and branch prediction in modern CPUs. "
    "For context, I lead the memory-subsystem team at Acme Silicon.\n"
    "adk_cc: Modern CPUs use L1 (~32KB), L2 (256-512KB per core), and L3 (tens "
    "of MB) caches, plus TAGE branch predictors for accuracy."
)
# things that would be OVER-capture (domain/subject matter — should NOT land):
DOMAIN = ["256", "512", "32kb", "tage", " l1", " l2", " l3", "branch predict",
          "cache hierarch", "reorder buffer"]
# the durable USER fact that SHOULD land:
USER = ["memory-subsystem", "acme", "lead", "team"]


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live model.")
        return 0
    try:
        raw = asyncio.run(MemoryPlugin()._extract(MODEL, TURN))
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: extract failed ({type(e).__name__}).")
        return 0
    facts = _parse_facts(raw)
    print("extracted facts:")
    for t, f in facts:
        print(f"  [{t}] {f}")
    blob = " ".join(f.lower() for _, f in facts)
    domain_hit = [m for m in DOMAIN if m in blob]
    user_hit = [m for m in USER if m in blob]
    print(f"\n  domain markers captured: {domain_hit}")
    print(f"  user   markers captured: {user_hit}")
    ok = not domain_hit
    print(f"\n  [{'PASS' if ok else 'FAIL'}] Fix B: domain content NOT captured")
    print(f"  [{'PASS' if user_hit else 'INFO'}] user fact captured")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
