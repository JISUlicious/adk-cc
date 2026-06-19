#!/usr/bin/env python3
"""Run the autonomous memory consolidation pass: episodic → semantic, per user.

This is the periodic background job behind the memory system's consolidation
(the design choice was a background cron). It folds each user's fresh episodic
captures into durable semantic facts (latest-wins + corroboration confidence +
supersession history) and archives stale, unaccessed semantic facts. Single-
user per scope, so no cross-user conflict handling (that's the wiki librarian).

Wire as cron / systemd timer, e.g.:

    # consolidate every hour
    0 * * * * cd /opt/adk-cc && .venv/bin/python scripts/memory_consolidator.py \
      --root /var/lib/adk-cc/.memory

Usage:
    python scripts/memory_consolidator.py [--root PATH] [--tenant ID ...]
                                          [--stale-days N] [--model] [--verbose]

  --root        memory root (default $ADK_CC_MEMORY_ROOT, else <workspace>/.memory)
  --tenant      consolidate only these tenant(s); repeatable. Default: all.
  --stale-days  archive semantic facts older + unaccessed than this (default 90).
  --model       use the agent model to synthesize merged facts (nicer prose);
                default is deterministic latest-wins (no model call).
  --compact     after consolidating, run LLM compaction (Fix F) to merge
                residual duplicate topics — parity with the in-process
                scheduler. Use this in the k8s CronJob.
  --verbose     DEBUG logging.

Exit codes: 0 ok, 1 bad --root, 2 invalid args.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from adk_cc.memory import consolidate_all, discover_tenants, memory_root_from_env

_log = logging.getLogger("memory_consolidator")


def _make_llm_synthesizer():
    """Model-backed synthesizer (shared impl in adk_cc.memory.synth)."""
    from adk_cc.agent import MODEL
    from adk_cc.memory.synth import make_llm_synthesizer

    return make_llm_synthesizer(MODEL)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Consolidate per-user memory.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--tenant", action="append", dest="tenants", default=None)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--compact", action="store_true",
                    help="after consolidating, run LLM compaction (Fix F): "
                         "merge residual duplicate topics across each user's "
                         "semantic tier. Implies model use.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    root = args.root or memory_root_from_env()
    if not os.path.isdir(root):
        _log.error("memory root does not exist: %s", root)
        return 1

    tenants = args.tenants or discover_tenants(root)
    if not tenants:
        _log.info("no tenant memory found under %s — nothing to do", root)
        return 0

    synthesizer = _make_llm_synthesizer() if args.model else None
    _log.info("consolidating %d tenant(s) under %s (model=%s)",
              len(tenants), root, "on" if args.model else "off")

    for tenant, rep in consolidate_all(
        root, tenants=tenants, synthesizer=synthesizer, stale_days=args.stale_days
    ):
        _log.info(
            "tenant=%s user=%s: episodic_seen=%d consolidated=%d "
            "(created=%d updated=%d) archived_stale=%d pruned=%d",
            tenant, rep.user_id, rep.episodic_seen, rep.topics_consolidated,
            rep.created, rep.updated, rep.archived_stale, rep.pruned_episodic,
        )

    # Fix F parity: optional LLM compaction pass (matches the in-process
    # scheduler). Separate from --model so a deployment can synth without it.
    if args.compact:
        from adk_cc.agent import MODEL
        from adk_cc.memory import compact_all

        for tenant, user_id, comp in compact_all(MODEL, root, tenants=tenants):
            if comp["merged"]:
                _log.info("tenant=%s user=%s: compacted %d duplicate topic(s) in %d group(s)",
                          tenant, user_id, comp["merged"], comp["groups"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
