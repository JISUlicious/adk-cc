"""Tool-call titles: the model labels every tool call for the frontend UI.

A `run_bash` call rendering as `python train.py ...` is accurate but opaque;
a model-written label like "Writing ML training script" makes the thread
scannable. Mirrors Claude Code's own Bash `description` input — a field that
exists purely for display.

Implemented at the PLUGIN layer, not as a per-tool input field, so it covers
EVERY tool — adk-cc tools, MCP tools, skill tools — without touching any
schema by hand:

  - `before_model_callback` appends an optional string `title` property to
    every function declaration in the outgoing request (both the
    `parameters_json_schema` dict form and the `types.Schema` form), plus a
    one-paragraph system-instruction telling the model how to fill it.
    Declarations are rebuilt fresh per request (LlmRequest.append_tools calls
    tool._get_declaration() each time), so per-request mutation is safe.

  - `before_tool_callback` pops `title` from the args so no tool (or MCP
    server with a strict schema) ever sees it. Safe for the event log: ADK
    deep-copies `function_call.args` before plugins/tools receive them
    (flows/llm_flows/functions.py), so the recorded functionCall event keeps
    the title — which is exactly where the frontend reads it from.

Collision safety: some tools declare their OWN `title` argument (task_create's
task title). Injection skips any declaration that already has a `title`
property, and stripping only removes the arg for tools WE injected — a native
`title` arg always passes through untouched.

Display-only by design: permission decisions, audit, and tool logic never key
off the title (it can lie; the real args can't).

Opt-in: registered in App.plugins only when ADK_CC_TOOL_TITLES=1 (adds a few
output tokens per tool call).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

_log = logging.getLogger(__name__)

_TITLE_ARG = "title"

_TITLE_DESCRIPTION = (
    "Optional short label for this call, shown to the user in the UI while "
    "it runs (3-6 words, present progressive, e.g. 'Writing ML training "
    "script'). Display-only; never put real parameters here."
)

TITLE_GUIDANCE = """
Every tool accepts an optional `title` argument: a short (3-6 word)
present-progressive phrase describing THIS specific call, shown to the user
in the UI while it runs (e.g. "Writing ML training script", "Searching for
config files"). Set it on every call. It is display-only — pass real
parameters in their own fields, never in the title."""


class ToolTitlePlugin(BasePlugin):
    """Injects an optional `title` arg into every tool declaration and strips
    it again before execution. See module docstring."""

    def __init__(self, *, name: str = "adk_cc_tool_title") -> None:
        super().__init__(name=name)
        # Tool names whose declaration WE extended. Stripping is limited to
        # this set so a tool's native `title` arg (task_create) is never eaten.
        # Add-only; shared across requests (same tool -> same shape).
        self._injected: set[str] = set()

    # ---- declaration injection ------------------------------------------

    def _inject_into_declaration(self, decl: Any) -> bool:
        """Add the optional `title` property to one FunctionDeclaration.
        Returns True if injected; False if skipped (native title / no params).
        """
        name = getattr(decl, "name", None)
        if not name:
            return False
        # Dict-schema form (AdkCcTool, ADK skill tools, most MCP tools).
        schema = getattr(decl, "parameters_json_schema", None)
        if isinstance(schema, dict):
            props = schema.setdefault("properties", {})
            if _TITLE_ARG in props:
                # Either a native title arg (skip forever) or our own from a
                # cached declaration (idempotent either way).
                return name in self._injected
            props[_TITLE_ARG] = {
                "type": "string",
                "description": _TITLE_DESCRIPTION,
            }
            return True
        # types.Schema form (FunctionTool-built declarations).
        params = getattr(decl, "parameters", None)
        if params is not None and getattr(params, "type", None) == types.Type.OBJECT:
            if params.properties is None:
                params.properties = {}
            if _TITLE_ARG in params.properties:
                return name in self._injected
            params.properties[_TITLE_ARG] = types.Schema(
                type=types.Type.STRING, description=_TITLE_DESCRIPTION
            )
            return True
        # No parameters at all (rare no-arg tool) — leave untouched.
        return False

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        for tool_obj in llm_request.config.tools or []:
            decls = getattr(tool_obj, "function_declarations", None)
            if not decls:
                continue
            for decl in decls:
                if self._inject_into_declaration(decl):
                    self._injected.add(decl.name)

        if not self._injected:
            return None  # nothing injected -> no guidance needed

        existing = llm_request.config.system_instruction
        if existing is None:
            llm_request.config.system_instruction = TITLE_GUIDANCE
        elif isinstance(existing, str):
            if TITLE_GUIDANCE not in existing:
                llm_request.config.system_instruction = existing + "\n" + TITLE_GUIDANCE
        else:
            try:
                parts = list(existing) if isinstance(existing, list) else [existing]
                parts.append(types.Part(text=TITLE_GUIDANCE))
                llm_request.config.system_instruction = parts
            except Exception:  # noqa: BLE001 — guidance is best-effort
                pass
        return None

    # ---- arg stripping ---------------------------------------------------

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        # Only strip what we injected; a native `title` arg passes through.
        # ADK deep-copies function_call.args before this hook, so the recorded
        # event (the frontend's source) keeps the title.
        if tool.name in self._injected and isinstance(tool_args, dict):
            tool_args.pop(_TITLE_ARG, None)
        return None
