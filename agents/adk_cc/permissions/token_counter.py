"""Shared prompt-token estimator that mirrors ADK's algorithm.

Before this module, `ContextGuardPlugin` used `litellm.token_counter`
(accurate per-model tokenization) and ADK's `EventsCompactionConfig`
used its own `_estimate_prompt_token_count` (chars/4 with a preference
for `usage_metadata.prompt_token_count`). The two layers could
disagree, with two failure modes:

  - Plugin counts 80k tokens (litellm-accurate); ADK counts 60k
    (chars/4 underestimate). Plugin WARNs / REJECTs while ADK thinks
    compaction isn't needed yet.
  - Plugin counts 60k; ADK counts 95k (overestimate via chars/4).
    ADK compacts aggressively while the plugin says fine.

This module exposes `estimate_prompt_tokens(...)` that uses ADK's
exact algorithm so the plugin and compaction logic agree on counts.
The plugin retains a separate litellm-based count for DEBUG
diagnostic logging (so operators can see the divergence when
investigating an "ADK didn't compact but I'm full" report), but its
threshold checks use this shared helper.

## Algorithm (mirrors `google.adk.apps.compaction._latest_prompt_token_count`)

1. If `session_events` includes any event whose `usage_metadata`
   carries `prompt_token_count`, return the MOST RECENT such count.
   This is the model's own count from a prior response — most
   accurate when available.
2. Otherwise, sum `len(part.text)` across `llm_request.contents`
   (matches `_count_text_chars_in_content`) and return
   `total_chars // 4`.

Returns `0` when neither source can produce a count — safer than
None for threshold-comparison callsites (the guard treats 0 as
"below threshold, proceed").

## Compatibility note

Mirrored from ADK 1.31.1 (`compaction.py:91-139`). If ADK changes its
counter in a future release, our `_estimate_prompt_token_count` may
diverge. Cheap to keep in sync — the algorithm is ~20 lines. We
deliberately mirror rather than import the private function: a
leading-underscore name in ADK could move or rename without warning.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def estimate_prompt_tokens(
    llm_request: Any,
    session_events: Optional[Iterable[Any]] = None,
) -> int:
    """Estimate prompt tokens. Mirrors ADK's algorithm.

    Args:
        llm_request: The `LlmRequest` about to be sent. Its
            `contents` are summed when no usage_metadata is
            available.
        session_events: Optional iterable of `Event` objects (most
            recent last). When provided, the most recent
            `event.usage_metadata.prompt_token_count` wins over the
            chars-based estimate.

    Returns:
        Integer token count. `0` when no source is available.
    """
    # 1. Prefer the model's own count from the latest usage metadata.
    if session_events is not None:
        events_list = list(session_events)
        for event in reversed(events_list):
            usage = getattr(event, "usage_metadata", None)
            if usage is None:
                continue
            count = getattr(usage, "prompt_token_count", None)
            if count is not None:
                return int(count)

    # 2. Fall back to ADK's chars/4 heuristic over llm_request.contents.
    return _estimate_from_request(llm_request)


def _estimate_from_request(llm_request: Any) -> int:
    """Sum text-part chars across the request's contents and divide by 4.

    Matches `apps/compaction._estimate_prompt_token_count` →
    `_count_text_chars_in_content`: only `part.text` contributes;
    function_call args, function_response payloads, inline data are
    NOT counted (mirroring ADK's choice). The under-count for tool-
    heavy turns is consistent between the two layers; that's the
    whole point of unifying.
    """
    if llm_request is None:
        return 0
    contents = getattr(llm_request, "contents", None) or []
    total_chars = 0
    for content in contents:
        total_chars += _count_text_chars_in_content(content)
    if total_chars <= 0:
        return 0
    return total_chars // 4


def _count_text_chars_in_content(content: Any) -> int:
    """Mirror of `apps/compaction._count_text_chars_in_content`."""
    if content is None:
        return 0
    parts = getattr(content, "parts", None)
    if not parts:
        return 0
    total = 0
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            total += len(text)
    return total


def estimate_prompt_tokens_full(
    llm_request: Any,
    session_events: Optional[Iterable[Any]] = None,
) -> int:
    """Payload-INCLUSIVE token estimate (chars/4) that ALSO counts
    function_call args and function_response payloads — the bytes ADK's
    text-only counter (and `estimate_prompt_tokens`) deliberately ignore.

    For a tool-heavy agent those payloads dominate, so the ADK-consistent
    estimate under-counts the real prompt. Use this for a safety REJECT decision
    or observability — NOT as the compaction trigger (that must stay aligned with
    ADK's own counter). Prefers the model's `usage_metadata.prompt_token_count`
    when available (the ground truth), same as `estimate_prompt_tokens`.
    """
    if session_events is not None:
        for event in reversed(list(session_events)):
            usage = getattr(event, "usage_metadata", None)
            count = getattr(usage, "prompt_token_count", None) if usage else None
            if count is not None:
                return int(count)
    if llm_request is None:
        return 0
    import json as _json

    total = 0
    for content in getattr(llm_request, "contents", None) or []:
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                total += len(text)
            fc = getattr(part, "function_call", None)
            if fc is not None:
                try:
                    total += len(_json.dumps(getattr(fc, "args", None) or {}, default=str))
                except Exception:
                    pass
            fr = getattr(part, "function_response", None)
            if fr is not None:
                try:
                    total += len(_json.dumps(getattr(fr, "response", None) or {}, default=str))
                except Exception:
                    pass
    return total // 4 if total > 0 else 0
