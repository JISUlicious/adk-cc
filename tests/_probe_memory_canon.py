"""Live probe: show episodic-topic fragmentation in raw memory contents, then
prove the prototype canonicalizer collapses semantic below episodic.

Quota-light: 3 capture extractions + 1 LLM-canon call. Skips if no live model.
Run:  .venv/bin/python tests/_probe_memory_canon.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

os.environ["ADK_CC_MEMORY"] = "1"
os.environ.setdefault("ADK_CC_MODEL_MAX_RPM", "30")  # pace the shared endpoint
_TMP = tempfile.mkdtemp(prefix="canon-root-")
os.environ["ADK_CC_MEMORY_ROOT"] = _TMP

from adk_cc.agent import MODEL
from adk_cc.memory import MemoryStore, consolidate_user
from adk_cc.memory.canonicalize import deterministic_canonical, make_llm_canonical
from adk_cc.plugins.memory import MemoryPlugin, _parse_facts

USER = "alice"

# Three turns. Each RESTATES the same durable fact (alice designs embedded
# low-power CPU cores) so we can watch the capture LLM slap a different topic
# slug on it each time; plus a couple of distinct facts (1 GHz clock, RISC-V).
TURNS = [
    "user: What does the wiki say about pipeline depth? For context, I design "
    "embedded low-power CPU cores.\n"
    "adk_cc: [called wiki_search] High-performance cores use ~14-stage "
    "pipelines; embedded low-power cores use shorter pipelines to save energy.",

    "user: Summarize out-of-order execution. We target a 1 GHz maximum clock, "
    "and remember I work on embedded low-power CPU core design.\n"
    "adk_cc: Out-of-order execution reorders instructions via a reorder buffer. "
    "For a 1 GHz embedded low-power core a small window keeps area and power low.",

    "user: Give me a concise recap of the wiki. As a reminder my focus is "
    "embedded low-power CPU cores and my team standardized on the RISC-V ISA.\n"
    "adk_cc: Concise recap of pipelines, branch prediction, and caches. Noting "
    "your embedded low-power RISC-V focus.",
]


async def _capture() -> list[tuple[str, str, str]]:
    plugin = MemoryPlugin()
    out: list[tuple[str, str, str]] = []
    for i, transcript in enumerate(TURNS):
        raw = await plugin._extract(MODEL, transcript)
        facts = _parse_facts(raw)
        print(f"  turn {i}: extracted {len(facts)} fact(s): "
              f"{[t for t, _ in facts]}")
        for topic, fact in facts:
            out.append((topic, fact, f"sess{i}"))
    return out


def _consolidate(records, mapping) -> list:
    """Fresh store: add episodics (topics remapped via `mapping` if given),
    consolidate, return semantic items."""
    root = tempfile.mkdtemp(prefix="canon-var-")
    s = MemoryStore.for_tenant("acme", root=root)
    for slug, text, src in records:
        topic = mapping.get(slug, slug) if mapping else slug
        s.add_episodic(USER, text, topic=topic, sources=[src])
    consolidate_user(s, USER)
    sem = s.list_semantic(USER)
    shutil.rmtree(root, ignore_errors=True)
    return sem


def _groups(mapping) -> dict:
    g: dict[str, list[str]] = {}
    for orig, canon in mapping.items():
        g.setdefault(canon, []).append(orig)
    return {k: v for k, v in g.items() if len(v) > 1}


def main() -> int:
    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY — canon probe skipped.")
        return 0

    try:
        print("== capture (real model extraction over 3 turns) ==")
        captured = asyncio.run(_capture())
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: capture failed ({type(e).__name__}: {e}).")
        return 0
    if not captured:
        print("SKIP: model extracted no facts.")
        return 0

    # Materialize the raw episodics once to read their SLUGGED topics.
    raw_root = tempfile.mkdtemp(prefix="canon-raw-")
    raw_store = MemoryStore.for_tenant("acme", root=raw_root)
    for slug, text, src in captured:
        raw_store.add_episodic(USER, text, topic=slug, sources=[src])
    epi = raw_store.list_episodic(USER)
    records = [(e.topic, e.text, (e.sources[0] if e.sources else "")) for e in epi]
    slugs = [e.topic for e in epi]
    shutil.rmtree(raw_root, ignore_errors=True)

    print("\n== raw episodic memory contents ==")
    for e in epi:
        print(f"  [{e.topic}] {e.text}")

    sem_raw = _consolidate(records, None)
    det_map = deterministic_canonical(slugs)
    sem_det = _consolidate(records, det_map)
    print("\n== building LLM canonical map (1 model call) ==")
    try:
        llm_map = make_llm_canonical(MODEL)(slugs)
    except Exception as e:  # noqa: BLE001
        print(f"  (LLM canon failed: {type(e).__name__}: {e}; using deterministic)")
        llm_map = det_map
    sem_llm = _consolidate(records, llm_map)

    print("\n== deterministic canon — merged groups ==")
    for canon, members in _groups(det_map).items():
        print(f"  {canon}  <=  {members}")
    print("== LLM canon — merged groups ==")
    for canon, members in _groups(llm_map).items():
        print(f"  {canon}  <=  {members}")

    n_epi = len(records)
    print("\n== RESULT: episodic vs semantic ==")
    print(f"  episodic captured           : {n_epi}")
    print(f"  semantic (raw, exact-slug)  : {len(sem_raw)}  topics={[s.topic for s in sem_raw]}")
    print(f"  semantic (deterministic)    : {len(sem_det)}  topics={[s.topic for s in sem_det]}")
    print(f"  semantic (LLM canon)        : {len(sem_llm)}  topics={[s.topic for s in sem_llm]}")

    collapsed = len(sem_det) < n_epi or len(sem_llm) < n_epi
    print(f"\n  [{'PASS' if collapsed else 'INFO'}] canonicalization collapses "
          f"semantic below episodic: raw={len(sem_raw)} det={len(sem_det)} "
          f"llm={len(sem_llm)} (episodic={n_epi})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
