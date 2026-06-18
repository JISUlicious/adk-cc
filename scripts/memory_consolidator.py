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
  --verbose     DEBUG logging.

Exit codes: 0 ok, 1 bad --root, 2 invalid args.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from adk_cc.memory import consolidate_all, discover_tenants, memory_root_from_env

_log = logging.getLogger("memory_consolidator")


def _make_llm_synthesizer():
    """Optional model-backed synthesizer: merge existing + new captures into a
    single coherent fact. Falls back to latest-wins on any failure."""
    from adk_cc.agent import MODEL
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing
    from google.genai import types

    _PROMPT = (
        "Merge these statements about one topic into ONE concise, current "
        "fact (1-2 sentences). Prefer the newest when they conflict; keep "
        "specifics. Output only the merged fact.\n\n"
        "Existing: {existing}\nNew (newest first):\n{new}"
    )

    def _synth(existing: Optional[str], new_texts: list[str]) -> str:
        prompt = _PROMPT.format(
            existing=existing or "(none)",
            new="\n".join(f"- {t}" for t in new_texts),
        )

        async def _call() -> str:
            req = LlmRequest(
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=types.GenerateContentConfig(),
            )
            out = ""
            async with Aclosing(MODEL.generate_content_async(req, stream=False)) as agen:
                async for resp in agen:
                    for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                        if not getattr(p, "thought", None) and getattr(p, "text", None):
                            out += p.text
            return out.strip()

        try:
            text = asyncio.run(asyncio.wait_for(_call(), timeout=45))
            return text or (new_texts[0] if new_texts else (existing or ""))
        except Exception as e:  # noqa: BLE001
            _log.warning("synth failed (%s) — latest-wins", type(e).__name__)
            return new_texts[0] if new_texts else (existing or "")

    return _synth


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Consolidate per-user memory.")
    ap.add_argument("--root", default=None)
    ap.add_argument("--tenant", action="append", dest="tenants", default=None)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--model", action="store_true")
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
            "(created=%d updated=%d) archived_stale=%d",
            tenant, rep.user_id, rep.episodic_seen, rep.topics_consolidated,
            rep.created, rep.updated, rep.archived_stale,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
