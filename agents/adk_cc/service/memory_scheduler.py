"""Optional in-process memory-consolidation scheduler.

Binds the periodic episodic→semantic consolidation pass (otherwise the
scripts/memory_consolidator.py cron) to the API server's lifespan, so a
deployment that doesn't run an external cron still grows semantic memory while
the server is up. Capture stays inline (MemoryPlugin.after_run); this only adds
the periodic *consolidation* half.

This is the TIME-BASED half of the hybrid: the staleness sweep plus a safety
net for users who never reach the capture-path threshold. The responsive half —
promote as soon as N unprocessed episodics stack up — lives in MemoryPlugin
(ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD). Set both for the full hybrid; they
serialize via memory.consolidation_lock. With the threshold doing prompt
promotion, this loop can run infrequently (e.g. daily) — mostly the sweep.

OFF by default. Enable by setting a positive interval:

    ADK_CC_MEMORY=1                             # memory subsystem on (required)
    ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S=3600   # run every hour (enables this)

Deterministic latest-wins synthesis only — NO model calls. This loop runs
unattended against the shared, rate-limited model endpoint, so model-backed
synthesis stays in the cron (`memory_consolidator.py --model`), never here.

Other knobs:
    ADK_CC_MEMORY_STALE_DAYS                    # archive threshold (default 90)
    ADK_CC_MEMORY_CONSOLIDATE_DELAY_S           # delay before the first pass
                                                # (default 60s; lets boot settle)

Single-worker assumption: with `uvicorn --workers N>1` every worker would run
its own loop and race on the memory files. For multi-worker production, leave
this off and run the external cron once instead. (The dev server is single
worker, so this is safe there.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

_log = logging.getLogger(__name__)

_DEFAULT_DELAY_S = 60.0
_DEFAULT_STALE_DAYS = 90


def _interval_s() -> float:
    """Consolidation period in seconds; <= 0 (or unset/invalid) → disabled."""
    try:
        return float(os.environ.get("ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S", ""))
    except ValueError:
        return 0.0


def _delay_s() -> float:
    try:
        return max(0.0, float(os.environ.get("ADK_CC_MEMORY_CONSOLIDATE_DELAY_S", "")))
    except ValueError:
        return _DEFAULT_DELAY_S


def _stale_days() -> int:
    try:
        return max(1, int(os.environ.get("ADK_CC_MEMORY_STALE_DAYS", "")))
    except ValueError:
        return _DEFAULT_STALE_DAYS


def scheduler_enabled() -> bool:
    return os.environ.get("ADK_CC_MEMORY") == "1" and _interval_s() > 0


def _synthesizer():
    """LLM synthesizer for periodic consolidation (off the hot path, so it's the
    default here). Disable with ADK_CC_MEMORY_SYNTH=deterministic."""
    if os.environ.get("ADK_CC_MEMORY_SYNTH") == "deterministic":
        return None
    try:
        from ..agent import MODEL
        from ..memory.synth import make_llm_synthesizer
        return make_llm_synthesizer(MODEL)
    except Exception:  # noqa: BLE001
        return None


def _compact_enabled() -> bool:
    return os.environ.get("ADK_CC_MEMORY_COMPACT") != "0"


async def _run_once() -> None:
    """One consolidation pass over all tenants/users, then (Fix F) an LLM
    compaction pass that re-merges residual topic fragmentation. Sync filesystem
    + model work runs in a thread; the shared lock keeps it off the capture-path
    threshold trigger."""
    from ..memory import consolidate_all, consolidation_lock, memory_root_from_env
    from ..memory.resolve import compact_all

    root = memory_root_from_env()
    synth = _synthesizer()

    def _run():
        with consolidation_lock:
            return consolidate_all(root, synthesizer=synth, stale_days=_stale_days())

    reports = await asyncio.to_thread(_run)
    if reports:
        topics = sum(r.topics_consolidated for _, r in reports)
        archived = sum(r.archived_stale for _, r in reports)
        pruned = sum(r.pruned_episodic for _, r in reports)
        _log.info(
            "memory consolidation: %d user(s), %d topic(s) consolidated, %d archived, %d pruned",
            len(reports), topics, archived, pruned,
        )

    if _compact_enabled():
        def _compact():
            from ..agent import MODEL
            with consolidation_lock:
                return compact_all(MODEL, root)
        try:
            comp = await asyncio.to_thread(_compact)
            merged = sum(c["merged"] for *_, c in comp)
            if merged:
                _log.info("memory compaction: merged %d duplicate topic(s) across %d user(s)",
                          merged, len(comp))
        except Exception as e:  # noqa: BLE001
            _log.warning("memory compaction skipped (%s: %s)", type(e).__name__, e)


async def _loop(interval: float, delay: float) -> None:
    if delay:
        await asyncio.sleep(delay)
    while True:
        try:
            await _run_once()
        except Exception as e:  # noqa: BLE001 — a bad pass must not kill the loop
            _log.warning("memory consolidation pass failed (%s: %s)", type(e).__name__, e)
        await asyncio.sleep(interval)


def make_consolidation_lifespan():
    """Return the server's ASGI lifespan. ALWAYS present (never None) because it
    warms the model delegate at startup — independent of the memory feature — so
    the first model call doesn't pay litellm's cold import on the event loop. It
    additionally runs the memory consolidation loop when that's enabled."""
    sched = scheduler_enabled()
    interval = _interval_s() if sched else 0.0
    delay = _delay_s() if sched else 0.0

    @contextlib.asynccontextmanager
    async def _lifespan(app):
        # Warm the LiteLlm delegate OFF the loop before serving traffic — the
        # cold litellm import (~hundreds of ms) would otherwise run on the loop
        # during the first request's first model call and stall health checks.
        # Best-effort; the request path's offloaded resolve is the fallback.
        try:
            from ..agent import MODEL
            await MODEL.warm()
        except Exception as e:  # noqa: BLE001 — warm-up must never break startup
            _log.debug("model warm-up skipped (%s: %s)", type(e).__name__, e)

        task = None
        if sched:
            task = asyncio.create_task(
                _loop(interval, delay), name="adk_cc_memory_consolidation"
            )
            _log.info(
                "memory consolidation scheduler started (every %.0fs, first pass in %.0fs)",
                interval, delay,
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                _log.info("memory consolidation scheduler stopped")

    return _lifespan
