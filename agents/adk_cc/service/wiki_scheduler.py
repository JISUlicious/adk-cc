"""Optional in-process wiki-librarian scheduler (#130 P1).

Binds the librarian merge pass (otherwise the scripts/wiki_librarian.py
cron) to the API server's lifespan, so a deployment without cron still
publishes inbox notes — the desktop app most of all, where the wiki is
hard-wired ON but nothing ever ran the librarian.

MODEL-ON BY DEFAULT. The wiki is an LLM system (Karpathy llm-wiki
lineage): conflict classification and page synthesis ARE the design, and
the heuristic is the degraded mode. Unlike memory consolidation's sweep,
librarian cost is event-driven — an idle tick has zero fresh claims and
makes ZERO model calls; cost scales with wiki_add captures, not wall
time. Per-claim classifier timeouts fall back to the heuristic and the
synthesis provenance guard falls back to the deterministic body, so a
dead endpoint degrades a run, never wedges the loop.
ADK_CC_WIKI_LIBRARIAN_MODEL=0 opts down to heuristic-only (rate-limited
API-key endpoints).

OFF by default. Enable with a positive interval:

    ADK_CC_WIKI=1
    ADK_CC_WIKI_LIBRARIAN_INTERVAL_S=900     # merge every 15 min

Other knobs:
    ADK_CC_WIKI_LIBRARIAN_DELAY_S            # boot settle (default 120)
    ADK_CC_WIKI_LIBRARIAN_MODEL              # =0: heuristic-only
    ADK_CC_WIKI_LIBRARIAN_PERSONAL_EVERY     # personal pass cadence
                                             # divisor (default 4) when
                                             # ADK_CC_PERSONAL_WIKI=1

Single-worker assumption, same as memory_scheduler: with N workers every
worker runs a loop — but the #130 P0 per-tenant flock makes the overlap
a SKIP (skipped_locked), never corruption; the extra loops are only
wasted wakeups. Multi-worker production should still prefer the cron.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Optional

from ..config.schema import env_bool

_log = logging.getLogger(__name__)

_DEFAULT_DELAY_S = 120.0
_DEFAULT_PERSONAL_EVERY = 4


def _interval_s() -> float:
    try:
        return float(os.environ.get("ADK_CC_WIKI_LIBRARIAN_INTERVAL_S", ""))
    except ValueError:
        return 0.0


def _delay_s() -> float:
    try:
        return max(0.0, float(
            os.environ.get("ADK_CC_WIKI_LIBRARIAN_DELAY_S", "")))
    except ValueError:
        return _DEFAULT_DELAY_S


def _personal_every() -> int:
    try:
        return max(1, int(
            os.environ.get("ADK_CC_WIKI_LIBRARIAN_PERSONAL_EVERY", "")))
    except ValueError:
        return _DEFAULT_PERSONAL_EVERY


def scheduler_enabled() -> bool:
    return env_bool("ADK_CC_WIKI") and _interval_s() > 0


def make_librarian_stack() -> tuple[Optional[Any], Optional[Any]]:
    """(classifier, synthesizer) per env — shared by the interval loop and
    the wiki_add threshold trigger (#130 P2). Model-backed by default;
    ADK_CC_WIKI_LIBRARIAN_MODEL=0 or any resolution failure degrades to
    (None, None) → the librarian's heuristic default."""
    if not env_bool("ADK_CC_WIKI_LIBRARIAN_MODEL", default=True):
        return None, None
    try:
        from ..agent import MODEL
        from ..wiki import LlmClassifier
        from ..wiki.runners import make_page_synthesizer

        return LlmClassifier(MODEL).aclassify, make_page_synthesizer(MODEL)
    except Exception as e:  # noqa: BLE001 — degraded beats dead
        _log.warning("wiki scheduler: model stack unavailable (%s: %s) — "
                     "heuristic-only", type(e).__name__, e)
        return None, None


async def run_librarian_once(
    root: str, *, personal: bool,
    classifier: Any = None, synthesizer: Any = None,
    tenants: Optional[list[str]] = None,
) -> list[Any]:
    """One merge pass over every tenant under `root` (optionally + the
    personal pass). Returns the MergeReports. Never raises."""
    from ..wiki import Librarian, PersonalWikiView, WikiStore
    from ..wiki.runners import discover_tenants

    reports: list[Any] = []
    try:
        for tenant in (tenants if tenants is not None
                       else discover_tenants(root)):
            store = WikiStore.for_tenant(tenant, root=root)
            rep = await Librarian(store, classifier=classifier,
                                  synthesizer=synthesizer).run()
            reports.append(rep)
            if rep.claims_seen or rep.skipped_locked:
                _log.info(
                    "wiki scheduler: tenant %s seen=%d actions=%s locked=%s",
                    tenant, rep.claims_seen, rep.actions, rep.skipped_locked)
            if personal:
                for uid in store.list_user_ids():
                    prep = await Librarian(
                        PersonalWikiView(store, uid), classifier=classifier,
                        synthesizer=synthesizer).run()
                    reports.append(prep)
                    if prep.claims_seen:
                        _log.info(
                            "wiki scheduler: tenant %s personal[%s] seen=%d "
                            "actions=%s", tenant, uid, prep.claims_seen,
                            prep.actions)
        try:
            from ..plugins.audit import emit_audit_event

            merged = sum(r.claims_seen for r in reports)
            if merged:
                emit_audit_event({
                    "event": "wiki_librarian_run", "trigger": "scheduler",
                    "claims_seen": merged, "runs": len(reports)})
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
        _log.warning("wiki scheduler: pass failed (%s: %s)",
                     type(e).__name__, e)
    return reports


async def _loop() -> None:
    from ..wiki import wiki_root_from_env

    interval = _interval_s()
    await asyncio.sleep(_delay_s())
    classifier, synthesizer = make_librarian_stack()
    _log.info("wiki scheduler: librarian loop up (interval=%.0fs, model=%s, "
              "personal=%s)", interval, classifier is not None,
              env_bool("ADK_CC_PERSONAL_WIKI"))
    tick = 0
    while True:
        tick += 1
        personal = (env_bool("ADK_CC_PERSONAL_WIKI")
                    and tick % _personal_every() == 0)
        try:
            await run_librarian_once(
                wiki_root_from_env(), personal=personal,
                classifier=classifier, synthesizer=synthesizer)
        except Exception as e:  # noqa: BLE001 — the loop outlives any tick
            _log.warning("wiki scheduler: tick failed (%s: %s)",
                         type(e).__name__, e)
        await asyncio.sleep(interval)


def make_wiki_lifespan(inner):
    """Wrap the server's existing lifespan (memory scheduler + model warm)
    with the librarian loop. ADK accepts ONE lifespan — this is the
    combinator."""

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        async with inner(app):
            task = None
            if scheduler_enabled():
                task = asyncio.create_task(_loop(),
                                           name="adk_cc_wiki_librarian")
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    return _lifespan
