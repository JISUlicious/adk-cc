"""Cached per-result summarization — the shared engine of the context plan.

Window = ONE tool result: each is bounded by its tool's own cap (~60KB), so
every summarization fits any summarizer window — no chunking machinery.
Summaries are keyed by content digest and computed ONCE EVER: session events
are immutable, so the same payload recurs in every later request; the cache
turns per-call cost into one-time cost. In-memory LRU with write-through to
disk so restarts don't re-pay.

The prompt is retrieval-oriented, not "summarize": these summaries exist to
keep FINDINGS alive (facts, numbers, URLs, identifiers) while shedding the
markup/boilerplate bulk — the whole objection to mechanical eviction.

Model resolution mirrors `_make_compaction_summarizer` (dedicated
ADK_CC_COMPACTION_MODEL, else the main-agent model) so whatever backend the
operator already trusts for compaction is what condenses results. Every
failure path returns None — callers fall back to a mechanical stub, which is
never worse than pre-summary behavior.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from ..config.schema import env_bool

_log = logging.getLogger(__name__)

_PROMPT = (
    "Condense this tool result for an AI coding agent that must keep "
    "working from it. PRESERVE facts, numbers, names, URLs, file paths, "
    "identifiers, error messages, and anything answer-relevant — verbatim "
    "where short. DROP boilerplate, navigation, markup, repetition. "
    "Output ONLY the condensed result, at most {max_words} words.\n\n"
    "Tool: {tool}\nResult:\n{body}"
)

_MAX_WORDS = 250
_BODY_CLIP = 120_000          # chars sent to the summarizer (≈ well in-window)
_MEM_MAX = 256                # LRU entries kept in memory
_NEG_TTL_S = 300.0            # a failed digest is not retried for this long

_mem: "OrderedDict[str, str]" = OrderedDict()
_neg: dict[str, float] = {}
_inflight: dict[str, "asyncio.Task[Optional[str]]"] = {}
_sem = asyncio.Semaphore(4)
_model = None                 # lazily built, config-keyed
_model_key: Optional[tuple] = None


def enabled() -> bool:
    return env_bool("ADK_CC_RESULT_SUMMARIES", default=True)


def _timeout_s() -> float:
    try:
        return max(0.0, float(os.environ.get("ADK_CC_COMPACTION_TIMEOUT_S", "30")))
    except ValueError:
        return 30.0


def _cache_dir() -> Optional[Path]:
    root = os.environ.get("ADK_CC_DESKTOP_DATA") or os.path.expanduser("~/.adk-cc")
    try:
        p = Path(root) / "context" / "summaries"
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:  # noqa: BLE001 — cache is an optimization, never a blocker
        return None


def _resolve_model():
    """Same precedence as `_make_compaction_summarizer`; instance cached and
    rebuilt only when the resolved config changes (operator edits env)."""
    global _model, _model_key
    key = (
        os.environ.get("ADK_CC_COMPACTION_MODEL") or os.environ.get("ADK_CC_MODEL")
        or "openai/gpt-4",
        os.environ.get("ADK_CC_COMPACTION_API_BASE", os.environ.get("ADK_CC_API_BASE")),
        os.environ.get("ADK_CC_COMPACTION_API_KEY", os.environ.get("ADK_CC_API_KEY", "")),
    )
    if _model is not None and key == _model_key:
        return _model
    from google.adk.models.lite_llm import LiteLlm

    kwargs = {}
    if key[1]:
        kwargs["api_base"] = key[1]
    if key[2]:
        kwargs["api_key"] = key[2]
    _model = LiteLlm(model=key[0], **kwargs)
    _model_key = key
    return _model


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _mem_put(digest: str, summary: str) -> None:
    _mem[digest] = summary
    _mem.move_to_end(digest)
    while len(_mem) > _MEM_MAX:
        _mem.popitem(last=False)


def _disk_get(digest: str) -> Optional[str]:
    d = _cache_dir()
    if d is None:
        return None
    try:
        f = d / f"{digest}.txt"
        return f.read_text("utf-8") if f.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _disk_put(digest: str, summary: str) -> None:
    d = _cache_dir()
    if d is None:
        return
    try:
        (d / f"{digest}.txt").write_text(summary, "utf-8")
    except Exception:  # noqa: BLE001
        pass


async def summarize(text: str, *, tool: str = "tool") -> Optional[str]:
    """A condensed form of `text`, or None (disabled / failed / timed out).
    Cache-first; concurrent requests for the same digest share one call."""
    if not enabled() or not text:
        return None
    digest = _digest(text)
    hit = _mem.get(digest)
    if hit is not None:
        _mem.move_to_end(digest)
        return hit
    disk = _disk_get(digest)
    if disk:
        _mem_put(digest, disk)
        return disk
    if _neg.get(digest, 0) > time.monotonic():
        return None
    task = _inflight.get(digest)
    if task is None:
        task = asyncio.create_task(_generate(digest, text, tool))
        _inflight[digest] = task
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a failed summary is just "no summary"
        return None


async def _generate(digest: str, text: str, tool: str) -> Optional[str]:
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    from ..memory.llm_text import final_response_text

    t0 = time.perf_counter()
    try:
        async with _sem:
            req = LlmRequest(
                contents=[types.Content(role="user", parts=[types.Part(
                    text=_PROMPT.format(max_words=_MAX_WORDS, tool=tool,
                                        body=text[:_BODY_CLIP]))])],
                config=types.GenerateContentConfig(),
            )
            timeout = _timeout_s()
            coro = final_response_text(_resolve_model(), req)
            raw = (await asyncio.wait_for(coro, timeout) if timeout
                   else await coro)
        summary = (raw or "").strip()
        if not summary:
            raise ValueError("empty summary")
        _mem_put(digest, summary)
        _disk_put(digest, summary)
        _log.info("result_summaries: condensed %d -> %d chars (%s, %.1fs)",
                  len(text), len(summary), tool, time.perf_counter() - t0)
        return summary
    except Exception as e:  # noqa: BLE001
        _neg[digest] = time.monotonic() + _NEG_TTL_S
        _log.warning("result_summaries: failed for %s (%s: %s)",
                     tool, type(e).__name__, str(e)[:150])
        return None
    finally:
        _inflight.pop(digest, None)


def _reset_for_test() -> None:
    global _model, _model_key
    _mem.clear()
    _neg.clear()
    _inflight.clear()
    _model = None
    _model_key = None
