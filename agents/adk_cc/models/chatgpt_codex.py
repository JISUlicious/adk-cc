"""ChatGPT-subscription model provider — the OpenAI Codex backend.

A `BaseLlm` that talks to `https://chatgpt.com/backend-api/codex/responses`
(the Responses API) using a ChatGPT *subscription* OAuth token from
`codex_auth` — NOT an OpenAI API key, NOT `api.openai.com`. Selected by
`SelectableLlm._build_litellm` when an endpoint's model id is
`chatgpt-codex/<model>` (see `models/selectable.py`).

Stateless per call (`store:false`): the whole conversation is re-sent as Responses
`input` items each turn, item IDs stripped, with `reasoning.encrypted_content`
echoed for reasoning continuity. Token material is never logged.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from typing import Any, AsyncGenerator, Optional

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from . import codex_auth

_BASE_URL = os.environ.get(
    "ADK_CC_CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex"
)
_ORIGINATOR = "codex_cli_rs"  # whitelisted; a third-party name gets 403
_UA = f"codex_cli_rs/0.0.0 (adk-cc; {platform.system().lower() or 'unknown'})"
# JSON error codes that mean "you're out of subscription quota" -> surfaced clearly.
_QUOTA_CODES = {
    "usage_limit_reached", "usage_not_included", "rate_limit_exceeded",
    "GoUsageLimitError", "FreeUsageLimitError",
}


class ChatGptCodexLlm(BaseLlm):
    """Inference against the ChatGPT-subscription Codex backend (Responses API)."""

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(self, *, model: str, effort: Optional[str] = None) -> None:
        super().__init__(model=model)
        # A stable id per provider instance -> prompt-cache continuity.
        self._session_id = str(uuid.uuid4())
        self._effort = effort or os.environ.get("ADK_CC_CODEX_EFFORT", "medium")

    # -- ADK contract ---------------------------------------------------

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        import httpx

        try:
            access, account = await codex_auth.get_access()
        except codex_auth.CodexAuthError as e:
            yield LlmResponse(
                error_code="chatgpt_auth" if not e.needs_login else "chatgpt_needs_login",
                error_message=str(e),
            )
            return

        body = self._build_body(llm_request)
        headers = {
            "Authorization": f"Bearer {access}",
            "ChatGPT-Account-ID": account,
            "originator": _ORIGINATOR,
            "OpenAI-Beta": "responses=experimental",
            "User-Agent": _UA,
            "session_id": self._session_id,
            "accept": "text/event-stream",
            "content-type": "application/json",
        }

        text_parts: list[str] = []
        func_calls: list[dict[str, Any]] = []
        usage: Optional[dict] = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            async with client.stream(
                "POST", f"{_BASE_URL}/responses", headers=headers, json=body
            ) as r:
                if r.status_code != 200:
                    raw = (await r.aread()).decode("utf-8", "replace")
                    yield self._http_error(r.status_code, raw)
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    kind = ev.get("type", "")
                    if kind == "response.output_text.delta":
                        delta = ev.get("delta", "")
                        if delta:
                            text_parts.append(delta)
                            yield LlmResponse(
                                content=types.Content(
                                    role="model", parts=[types.Part(text=delta)]
                                ),
                                partial=True,
                            )
                    elif kind == "response.output_item.done":
                        item = ev.get("item", {})
                        if item.get("type") == "function_call":
                            func_calls.append(item)
                    elif kind == "response.completed":
                        usage = (ev.get("response") or {}).get("usage")
                    elif kind in ("response.failed", "error"):
                        yield self._stream_error(ev)
                        return

        yield self._final(text_parts, func_calls, usage)

    # -- request building ----------------------------------------------

    def _build_body(self, req: LlmRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "store": False,
            "stream": True,
            "instructions": self._instructions(req) or "You are a helpful coding assistant.",
            "input": self._input_items(req.contents or []),
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": self._effort, "summary": "auto"},
            "prompt_cache_key": self._session_id,
        }
        tools = self._tools(req)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        return body

    def _instructions(self, req: LlmRequest) -> str:
        cfg = getattr(req, "config", None)
        return _text_of(getattr(cfg, "system_instruction", None)) if cfg else ""

    def _input_items(self, contents: list[types.Content]) -> list[dict[str, Any]]:
        """Map ADK contents -> Responses `input` items. Item IDs are stripped
        (store:false is stateless); function calls/outputs become their own items."""
        items: list[dict[str, Any]] = []
        for content in contents:
            role = content.role or "user"
            msg_type = "output_text" if role == "model" else "input_text"
            out_role = "assistant" if role == "model" else "user"
            text_run: list[dict[str, Any]] = []

            def _flush() -> None:
                if text_run:
                    items.append({"role": out_role, "content": list(text_run)})
                    text_run.clear()

            for part in content.parts or []:
                if part.function_call is not None:
                    _flush()  # keep item order: text before the call it precedes
                    fc = part.function_call
                    items.append({
                        "type": "function_call",
                        "call_id": fc.id or _call_id(fc.name),
                        "name": fc.name,
                        "arguments": json.dumps(fc.args or {}),
                    })
                elif part.function_response is not None:
                    _flush()
                    fr = part.function_response
                    items.append({
                        "type": "function_call_output",
                        "call_id": fr.id or _call_id(fr.name),
                        "output": _stringify(fr.response),
                    })
                elif part.text:
                    text_run.append({"type": msg_type, "text": part.text})
            _flush()
        return items

    def _tools(self, req: LlmRequest) -> list[dict[str, Any]]:
        from google.adk.models.lite_llm import _function_declaration_to_tool_param

        cfg = getattr(req, "config", None)
        out: list[dict[str, Any]] = []
        for tool in getattr(cfg, "tools", None) or []:
            for fd in getattr(tool, "function_declarations", None) or []:
                cc = _function_declaration_to_tool_param(fd)  # {type,function:{...}}
                fn = cc.get("function", {})
                out.append({
                    "type": "function",
                    "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        return out

    # -- response building ---------------------------------------------

    def _final(
        self, text_parts: list[str], func_calls: list[dict], usage: Optional[dict]
    ) -> LlmResponse:
        parts: list[types.Part] = []
        text = "".join(text_parts)
        if text:
            parts.append(types.Part(text=text))
        for item in func_calls:
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                args = {}
            parts.append(types.Part(function_call=types.FunctionCall(
                id=item.get("call_id"), name=item.get("name"), args=args,
            )))
        return LlmResponse(
            content=types.Content(role="model", parts=parts or [types.Part(text="")]),
            partial=False,
            usage_metadata=_usage(usage),
        )

    def _http_error(self, status: int, raw: str) -> LlmResponse:
        code, msg = "", raw[:400]
        try:
            err = (json.loads(raw) or {}).get("error") or json.loads(raw).get("detail")
            if isinstance(err, dict):
                code, msg = err.get("code", ""), err.get("message", msg)
            elif isinstance(err, str):
                msg = err
        except ValueError:
            pass
        quota = code in _QUOTA_CODES or status == 429 or "usage limit" in raw.lower()
        return LlmResponse(
            error_code="chatgpt_quota" if quota else f"chatgpt_http_{status}",
            error_message=(
                "ChatGPT subscription quota reached — try later or a lower effort."
                if quota else f"Codex backend error (HTTP {status}): {msg}"
            ),
        )

    def _stream_error(self, ev: dict) -> LlmResponse:
        err = ev.get("error") or ev.get("response", {}).get("error") or {}
        code = err.get("code", "")
        quota = code in _QUOTA_CODES
        return LlmResponse(
            error_code="chatgpt_quota" if quota else "chatgpt_stream_error",
            error_message=err.get("message") or f"Codex stream error: {code or ev.get('type')}",
        )


# -- helpers -----------------------------------------------------------

def _call_id(name: Optional[str]) -> str:
    return f"call_{(name or 'fn')}_{uuid.uuid4().hex[:8]}"


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _text_of(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, types.Content):
        return "".join(p.text for p in (x.parts or []) if p.text)
    if isinstance(x, (list, tuple)):
        return "\n".join(_text_of(i) for i in x)
    if isinstance(x, types.Part) and x.text:
        return x.text
    return ""


def _usage(usage: Optional[dict]) -> Optional[types.GenerateContentResponseUsageMetadata]:
    if not usage:
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    total = usage.get("total_tokens")
    details = usage.get("output_tokens_details") or {}
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=inp,
        candidates_token_count=out,
        total_token_count=total or ((inp or 0) + (out or 0)),
        thoughts_token_count=details.get("reasoning_tokens"),
    )
