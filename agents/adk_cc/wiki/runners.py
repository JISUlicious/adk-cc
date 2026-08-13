"""Reusable librarian runners (#130): shared by the cron script and the
in-process scheduler. The wiki is an LLM system (Karpathy llm-wiki
lineage) — the model-backed classifier + page synthesizer are the default
stack everywhere; the heuristic is the degraded mode, not the design."""
from __future__ import annotations

import os
from typing import Optional


def discover_tenants(root: str) -> list[str]:
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


def make_page_synthesizer(model):
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
