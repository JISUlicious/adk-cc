"""Collect the text of a non-streaming LLM call, robustly.

Found live (game-build dogfooding): some delegates (the chatgpt-codex
adapter) yield the COMPLETE response more than once even with stream=False.
The naive `out += part.text` collector then doubles the result — which
poisoned memory in two ways at once: every synthesized semantic fact was its
own sentence twice, and the capture extractor's raw output glued copy-1's
last line to copy-2's first line ("...fact.TOPIC: next | ..."), yielding
duplicate facts plus prompt-format fragments leaking into fact bodies.

Rule here: PARTIAL chunks accumulate; every COMPLETE (non-partial) response
REPLACES the accumulator. Yielding the full text twice then converges to one
copy, while true streaming still assembles correctly.
"""

from __future__ import annotations

from google.adk.utils.context_utils import Aclosing


async def final_response_text(model, req) -> str:
    """Run `model.generate_content_async(req, stream=False)` and return the
    final text (thoughts excluded)."""
    partial_buf = ""
    final: str | None = None
    async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
        async for resp in agen:
            parts = getattr(getattr(resp, "content", None), "parts", None) or []
            text = "".join(
                p.text for p in parts
                if getattr(p, "text", None) and not getattr(p, "thought", None)
            )
            if not text:
                continue
            if getattr(resp, "partial", False):
                partial_buf += text
            else:
                final = text
    return (final if final is not None else partial_buf).strip()
