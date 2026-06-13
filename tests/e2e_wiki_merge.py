"""E2E: multi-user wiki merge with the REAL model (no mocks).

Exercises the actual conflict path the user asked about — "what happens when
a user inbox and the domain base conflict?" — end to end, with the live
model doing the classification:

  domain:  "gpt-4-context" = GPT-4 context window is 8192 tokens.
  alice  → 128000 tokens (CITED)         } corroborating, cited
  bob    → 128000 tokens (CITED)         }
  carol  → NOT 8192, only 2048 (UNCITED) → explicit conflict, uncited

We assert MODEL-INDEPENDENT safety invariants, not a specific classification
label: a live model varies run to run (the local kimi-k2.6 classified
carol's claim as contradiction / refinement / supersession on different
runs), so per-label behaviour — quarantine vs contest vs overturn — is pinned
deterministically in test_wiki_conflict.py instead. What MUST hold here,
regardless of how the model labels each claim:

  1. the live merge runs without a fatal error
  2. NO SILENT LOSS — every seeded value (128000, 2048) lands somewhere
     traceable: the domain page (with provenance) or the review queue
  3. NO SILENT OVERWRITE — the original fact (8192) is preserved or recorded
     in a supersession validity window, never just deleted
  4. every published bullet carries a provenance marker (auditable trail)
  5. recall surfaces the merged page for a relevant query

The model's actual classification + whether carol was held/contested is
PRINTED for inspection, not asserted.

Skips gracefully (exit 0) when no live model is reachable / responsive —
matching the "verify with real e2e, skip when unavailable" rule. Run:

    .venv/bin/python tests/e2e_wiki_merge.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# Use the REAL .env (real API key) — do NOT skip dotenv here.
os.environ["ADK_CC_WIKI"] = "1"
_TMP = tempfile.mkdtemp(prefix="wiki-e2e-")
os.environ["ADK_CC_WIKI_ROOT"] = _TMP

from adk_cc.memory import search as searchlib
from adk_cc.memory.librarian import Librarian, LlmClassifier
from adk_cc.memory.page import Page
from adk_cc.memory.store import WikiStore


_PROBE_TIMEOUT_S = 30


def _model_available(model) -> bool:
    """Probe the RAW model (not via LlmClassifier, which swallows errors and
    falls back to the heuristic — that would mask an unreachable model and
    silently test the wrong thing). False on transport failure OR if the
    endpoint doesn't answer within _PROBE_TIMEOUT_S (LiteLlm has no short
    default timeout, so a hung endpoint must SKIP, not hang the test)."""
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing
    from google.genai import types

    async def _probe() -> str:
        req = LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text="say ok")])],
            config=types.GenerateContentConfig(),
        )
        out = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                    if getattr(p, "text", None):
                        out += p.text
        return out

    try:
        text = asyncio.run(asyncio.wait_for(_probe(), timeout=_PROBE_TIMEOUT_S))
        return bool(text.strip())
    except asyncio.TimeoutError:
        print(f"  (model probe timed out after {_PROBE_TIMEOUT_S}s)")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  (model probe failed: {type(e).__name__}: {e})")
        return False


def main() -> int:
    ok = True

    def check(name, cond, detail):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and cond

    key = os.environ.get("ADK_CC_API_KEY", "")
    if not key or key == "stub":
        print("SKIP: no live ADK_CC_API_KEY configured — wiki merge e2e skipped.")
        return 0

    from adk_cc.agent import MODEL

    if not _model_available(MODEL):
        print("SKIP: live model unreachable — wiki merge e2e skipped.")
        return 0

    st = WikiStore.for_tenant("acme").ensure()
    st.set_setting("corroboration_n", 2)
    st.write_domain_page(
        Page("gpt-4-context", {"title": "GPT-4 Context"},
             "GPT-4's context window is 8192 tokens.\n")
    )
    st.add_inbox("alice", "GPT-4's context window is 128000 tokens.",
                 topic="gpt-4-context", sources=["openai-docs"])
    st.add_inbox("bob", "GPT-4's context window is 128000 tokens.",
                 topic="gpt-4-context", sources=["openai-blog"])
    st.add_inbox("carol", "The wiki is wrong: GPT-4's context window is NOT "
                 "8192 tokens — it is only 2048 tokens.",
                 topic="gpt-4-context")  # uncited, explicit conflict

    print(f"seeded tenant 'acme' under {_TMP}")
    # Bound each classification so a looping/slow endpoint degrades to the
    # heuristic instead of stalling the test (safety invariants hold either way).
    classifier = LlmClassifier(MODEL, timeout_s=25).aclassify
    report = asyncio.run(Librarian(st, classifier=classifier).run())
    print(f"merge report: actions={report.actions} "
          f"quarantined={len(report.quarantined)} errors={report.errors}")

    page = st.read_domain_page("gpt-4-context")
    body = page.body if page else ""
    quarantine_blob = " ".join(str(r) for r in st.list_quarantine(pending_only=False))

    # We assert the model-INDEPENDENT safety properties of the pipeline (the
    # exact classification label is the live model's call and varies run to
    # run — quarantine/contest/overturn behaviour per label is proven
    # deterministically in test_wiki_conflict.py). What MUST hold regardless:

    # 1. the live merge runs clean
    check("merge ran without fatal error", not report.errors, str(report.errors))

    # 2. NO SILENT LOSS: every seeded claim's distinctive value lands somewhere
    #    traceable — in the domain page (with provenance) or the review queue.
    def _present(val: str) -> bool:
        return val in body or val in quarantine_blob
    check("alice/bob's value (128000) is captured, not dropped",
          _present("128000"), f"in_body={'128000' in body}")
    check("carol's conflicting value (2048) is captured, not dropped",
          _present("2048"),
          f"in_body={'2048' in body} in_quarantine={'2048' in quarantine_blob}")

    # 3. NO SILENT OVERWRITE: the original domain fact is preserved or
    #    explicitly superseded (a validity window records it) — never just
    #    deleted without a trace.
    validity = (page.frontmatter.get("validity") if page else None) or []
    preserved = "8192" in body or any("8192" in str(v) for v in validity)
    check("original domain fact (8192) preserved or explicitly superseded",
          preserved, f"in_body={'8192' in body} validity_windows={len(validity)}")

    # 4. every published bullet carries provenance (auditable trail)
    bullets = body.count("\n- ")
    prov_markers = body.count("_(by ")
    check("every published claim carries provenance",
          prov_markers >= bullets,
          f"bullets={bullets} provenance_markers={prov_markers}")

    # 5. recall surfaces the page
    hits = searchlib.search(st, "GPT-4 context window", limit=5)
    check("recall surfaces the merged page",
          any(h.slug == "gpt-4-context" for h in hits),
          f"hits={[h.slug for h in hits]}")

    # informational: how the live model actually classified the conflict
    carol_held = len(st.list_inbox("carol")) >= 1
    print(f"\n[info] live-model outcome: actions={report.actions} "
          f"carol_held={carol_held} quarantined={len(report.quarantined)} "
          f"contested={bool(page and page.contested)}")
    print("\n--- final domain page ---")
    print(body)
    print("wiki-merge e2e " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
