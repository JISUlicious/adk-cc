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

from adk_cc.memory import Librarian, LlmClassifier, WikiStore, wiki_root_from_env

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


async def _run(root: str, tenants: list[str], use_model: bool) -> int:
    classifier = None
    if use_model:
        from adk_cc.agent import MODEL  # constructs the configured SelectableLlm

        classifier = LlmClassifier(MODEL).aclassify

    overall = 0
    for tenant in tenants:
        store = WikiStore.for_tenant(tenant, root=root)
        lib = Librarian(store, classifier=classifier)
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
    return overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Merge user inboxes into the domain wiki.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--tenant", action="append", dest="tenants", default=None)
    ap.add_argument("--no-model", action="store_true")
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
    return asyncio.run(_run(root, tenants, use_model=not args.no_model))


if __name__ == "__main__":
    sys.exit(main())
