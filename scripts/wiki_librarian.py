#!/usr/bin/env python3
"""Run the offline wiki librarian: merge user inboxes into the shared domain
wiki, per tenant. This is the periodic job behind requirement 5 ("user-scope
docs are periodically merged/linted into domain scope").

The librarian is the SINGLE writer to each tenant's domain wiki — running it
out-of-band (here) is what keeps concurrent user captures from corrupting the
shared wiki with un-resolvable conflicts. It never runs inside a user turn.

Wire as cron / systemd timer in production, e.g.:

    # merge every 15 minutes
    */15 * * * * cd /opt/adk-cc && \
      .venv/bin/python scripts/wiki_librarian.py --root /var/lib/adk-cc/.wiki

Usage:
    python scripts/wiki_librarian.py [--root PATH] [--tenant ID ...]
                                     [--no-model] [--verbose]

  --root      wiki root (default: $ADK_CC_WIKI_ROOT, else <workspace>/.wiki)
  --tenant    merge only these tenant(s); repeatable. Default: every tenant
              tree found under the root.
  --no-model  use the deterministic heuristic classifier (no LLM call). The
              default uses the agent's configured model for nuanced conflict
              classification.
  --verbose   DEBUG logging.

Exit codes: 0 ok (possibly with per-slug errors reported), 1 bad --root,
2 invalid arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from adk_cc.wiki import Librarian, LlmClassifier, WikiStore, wiki_root_from_env

_log = logging.getLogger("wiki_librarian")


def _discover_tenants(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        # a tenant tree has a domain/ or users/ subdir
        if os.path.isdir(p) and (
            os.path.isdir(os.path.join(p, "domain"))
            or os.path.isdir(os.path.join(p, "users"))
        ):
            out.append(name)
    return out


def _make_page_synthesizer(model):
    """LLM page synthesizer: rewrite the deterministic page body into coherent
    prose, PRESERVING every `_(by …)` provenance marker verbatim (the
    librarian's guard rejects a synthesis that drops them). Falls back to the
    deterministic body on any failure."""
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing
    from google.genai import types

    _PROMPT = (
        "Rewrite this wiki page into clear, well-organized prose about "
        "'{slug}'. Keep ALL facts and copy EVERY `_(by …)` provenance marker "
        "verbatim. Do not invent anything. Output only the page body.\n\n{body}"
    )

    async def _synth(slug: str, body: str) -> str:
        prompt = _PROMPT.format(slug=slug, body=body)
        req = LlmRequest(
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(),
        )
        out = ""
        async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
            async for resp in agen:
                for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                    if not getattr(p, "thought", None) and getattr(p, "text", None):
                        out += p.text
        return out

    return _synth


async def _run(root: str, tenants: list[str], use_model: bool, compact: bool) -> int:
    classifier = None
    synthesizer = None
    verifier = None
    compact_resolver = None
    if use_model:
        from adk_cc.agent import MODEL  # constructs the configured SelectableLlm
        from adk_cc.wiki import make_llm_entity_resolver, make_llm_merge_verifier

        classifier = LlmClassifier(MODEL).aclassify
        synthesizer = _make_page_synthesizer(MODEL)
        if compact:
            # compaction merges across the published domain → guard it with a
            # verifier, and use the LLM resolver so it catches true aliases (not
            # just hyphenation variants).
            verifier = make_llm_merge_verifier(MODEL)
            compact_resolver = make_llm_entity_resolver(MODEL)

    overall = 0
    for tenant in tenants:
        store = WikiStore.for_tenant(tenant, root=root)
        lib = Librarian(store, classifier=classifier, synthesizer=synthesizer)
        report = await lib.run()
        _log.info(
            "tenant %s: seen=%d actions=%s slugs=%d quarantined=%d "
            "skipped(no_promote=%d, queued=%d) errors=%d",
            tenant, report.claims_seen, report.actions, len(report.slugs_touched),
            len(report.quarantined), report.skipped_no_promote,
            report.skipped_queued, len(report.errors),
        )
        for err in report.errors:
            _log.warning("tenant %s slug error: %s", tenant, err)
            overall = max(overall, 0)  # per-slug errors don't fail the job
        if compact:
            # A separate librarian for the compaction pass: LLM resolver +
            # verifier (when --model), deterministic hyphenation dedup otherwise.
            comp_lib = Librarian(store, resolver=compact_resolver, verifier=verifier)
            crep = await comp_lib.compact()
            _log.info(
                "tenant %s: compaction merged=%d group(s)=%d pages %d→%d",
                tenant, crep.merged, crep.groups, crep.pages_before, crep.pages_after,
            )
    return overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge user inboxes into the domain wiki.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--tenant", action="append", dest="tenants", default=None)
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--compact", action="store_true",
                    help="after merging, re-dedup the published domain pages "
                         "(verified). With a model, catches true aliases; "
                         "without, hyphenation variants.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    root = args.root or wiki_root_from_env()
    if not os.path.isdir(root):
        _log.error("wiki root does not exist: %s", root)
        return 1

    tenants = args.tenants or _discover_tenants(root)
    if not tenants:
        _log.info("no tenant wikis found under %s — nothing to do", root)
        return 0

    _log.info("merging %d tenant(s) under %s (model=%s)",
              len(tenants), root, "off" if args.no_model else "on")
    return asyncio.run(_run(root, tenants, use_model=not args.no_model, compact=args.compact))


if __name__ == "__main__":
    sys.exit(main())
