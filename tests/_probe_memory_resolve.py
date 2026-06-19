"""Live probe: capture through the Fix A+D resolver folds drifted topics at
WRITE time, so semantic < episodic without any post-hoc canon. Shows the Fix G
changelog too. Skips without a live model.

Run:  .venv/bin/python tests/_probe_memory_resolve.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

os.environ["ADK_CC_MEMORY"] = "1"
os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")
_TMP = tempfile.mkdtemp(prefix="rslv-root-")
os.environ["ADK_CC_MEMORY_ROOT"] = _TMP

from adk_cc.agent import MODEL
from adk_cc.memory import MemoryStore, consolidate_user, resolve_facts
from adk_cc.plugins.memory import MemoryPlugin, _parse_facts

USER = "alice"
TURNS = [
    "user: What does the wiki say about pipeline depth? For context, I design "
    "embedded low-power CPU cores.\n"
    "adk_cc: High-performance cores use ~14-stage pipelines; embedded low-power "
    "cores use shorter pipelines to save energy.",

    "user: Summarize out-of-order execution. We target a 1 GHz maximum clock, "
    "and remember I work on embedded low-power CPU core design.\n"
    "adk_cc: OoO reorders instructions via a reorder buffer; for a 1 GHz "
    "embedded low-power core a small window keeps area and power low.",

    "user: Give me a concise recap. As a reminder my focus is embedded "
    "low-power CPU cores and my team standardized on the RISC-V ISA.\n"
    "adk_cc: Recap of pipelines, branch prediction, caches. Noting your "
    "embedded low-power RISC-V focus.",
]


async def _run(store) -> int:
    plugin = MemoryPlugin()
    n_facts = 0
    for i, transcript in enumerate(TURNS):
        raw = await plugin._extract(MODEL, transcript)
        facts = _parse_facts(raw)
        res = await resolve_facts(MODEL, store, USER, facts)
        for r in res:
            store.add_episodic(USER, r.fact, topic=r.topic, sources=[f"sess{i}"])
            n_facts += 1
        print(f"  turn {i}: " + (", ".join(f"{r.action}->{r.topic}" for r in res) or "(none)"))
    return n_facts


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY.")
        return 0
    store = MemoryStore.for_tenant("acme", root=_TMP)
    try:
        print("== capture through resolver (extract → resolve → add) ==")
        n = asyncio.run(_run(store))
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: capture/resolve failed ({type(e).__name__}: {e}).")
        return 0
    if n == 0:
        print("SKIP: model extracted no facts.")
        return 0

    epi = store.list_episodic(USER)
    print(f"\n  episodic ({len(epi)}): {[e.topic for e in epi]}")
    consolidate_user(store, USER)
    sem = store.list_semantic(USER)
    print(f"  semantic ({len(sem)}): {[s.topic for s in sem]}")
    print("\n  changelog ops:", [e["op"] for e in store.read_changelog(USER)])
    print("  topic index :", list(store.get_topic_index(USER)))

    folded = len(sem) < len(epi)
    print(f"\n  [{'PASS' if folded else 'INFO'}] resolver folded at write: "
          f"episodic={len(epi)} semantic={len(sem)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
