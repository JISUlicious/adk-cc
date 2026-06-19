"""Model-backed memory synthesizer, shared by the cron, the in-process
scheduler, and compaction. Merges statements about one topic into a single
current fact; falls back to latest-wins on any failure. The model is passed in
(no import coupling to the agent)."""

from __future__ import annotations

import asyncio
from typing import Optional

_PROMPT = (
    "Merge these statements about one topic into ONE concise, current fact "
    "(1-2 sentences). Prefer the newest when they conflict; keep specifics. "
    "Output only the merged fact.\n\n"
    "Existing: {existing}\nNew (newest first):\n{new}"
)


def make_llm_synthesizer(model, *, timeout_s: float = 45.0):
    """Return a Synthesizer(existing, new_texts) -> str backed by `model`."""
    from google.adk.models.llm_request import LlmRequest
    from google.adk.utils.context_utils import Aclosing
    from google.genai import types

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
            async with Aclosing(model.generate_content_async(req, stream=False)) as agen:
                async for resp in agen:
                    for p in (getattr(getattr(resp, "content", None), "parts", None) or []):
                        if not getattr(p, "thought", None) and getattr(p, "text", None):
                            out += p.text
            return out.strip()

        try:
            text = asyncio.run(asyncio.wait_for(_call(), timeout=timeout_s))
            return text or (new_texts[0] if new_texts else (existing or ""))
        except Exception:  # noqa: BLE001 — synthesis failure ⇒ latest-wins
            return new_texts[0] if new_texts else (existing or "")

    return _synth
