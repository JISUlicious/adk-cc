"""Inject a `response_schema` into `ask_user_question` function-call args
so that `adk web`'s bundled UI renders a structured form (one input per
question) instead of a free-form text/JSON textarea.

Why this exists
---------------

The bundled `adk web` UI's `app-long-running-response` component (see
`google/adk/cli/browser/main-*.js`) branches on
`functionCall.args.response_schema`:

  - When `response_schema.type === "object"` with `properties`, it
    auto-builds a form: one input per property. Submitting sends the
    response back as `{<property>: <value>}` directly.
  - Otherwise, it falls back to a free-form text/JSON textarea where
    the operator has to type a JSON response.

`ask_user_question`'s args are `{questions: [...]}` — no
`response_schema` — so users see the fallback textarea. The fix could
live on the tool's input contract, but that asks the LLM to
construct a schema accurately (fragile, and conceptually wrong — the
schema is about the OUTPUT shape, not the input). Instead we mutate the
function-call args after the LLM emits them, before ADK builds the
event the UI consumes.

`after_model_callback` is the right hook: it sees the `LlmResponse`
before the runner builds the function-call event for the UI. Mutating
the function_call's `args` in place is picked up by the rest of the
flow (the event's content carries the same `args` reference).

The injected schema is invisible to the tool itself — `_execute` reads
the validated `AskUserQuestionArgs`, not the raw args dict, so the
`response_schema` key doesn't show up in `args`. The tool's
`awaiting_user_input` first-call result is unchanged; the bundled UI
just renders a real form on top of the function-CALL event card.
"""
from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin


class AskUserQuestionUiHintPlugin(BasePlugin):
    """Annotates `ask_user_question` function-calls with a UI-side response_schema.

    The schema is derived from the LLM-supplied `questions` list — one
    property per question, with `enum` constraints from each question's
    `options`. `multi_select=True` produces an `array` property with the
    same enum on `items`.

    Registered alongside the other adk-cc plugins; affects both `adk web`
    dev runs and the FastAPI factory.
    """

    def __init__(self, name: str = "ask_user_question_ui_hint") -> None:
        super().__init__(name=name)

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> Optional[LlmResponse]:
        content = getattr(llm_response, "content", None)
        if content is None or not getattr(content, "parts", None):
            return None
        for part in content.parts:
            fc = getattr(part, "function_call", None)
            if fc is None or fc.name != "ask_user_question":
                continue
            args = dict(fc.args or {})
            # Don't clobber a model-provided schema (rare; safety net).
            if isinstance(args.get("response_schema"), dict):
                continue
            schema = _build_response_schema(args.get("questions") or [])
            if schema is None:
                continue
            args["response_schema"] = schema
            fc.args = args
        # Mutation is in place; returning None tells ADK to use the
        # original llm_response (which now carries our mutations).
        return None


def _build_response_schema(questions: list[Any]) -> Optional[dict]:
    """Build a JSON schema with one property per question.

    Returns None when there's nothing useful to expose (no questions or
    every question is missing required fields).
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        qtext = q.get("question") or ""
        if not qtext:
            continue
        options = q.get("options") or []
        labels = [
            o.get("label")
            for o in options
            if isinstance(o, dict) and isinstance(o.get("label"), str)
        ]
        if not labels:
            # No selectable options — fall back to a free-form string.
            schema_for_q: dict[str, Any] = {"type": "string", "description": qtext}
        elif q.get("multi_select"):
            schema_for_q = {
                "type": "array",
                "items": {"type": "string", "enum": labels},
                "description": qtext,
            }
        else:
            schema_for_q = {
                "type": "string",
                "enum": labels,
                "description": qtext,
            }
        properties[qtext] = schema_for_q
        required.append(qtext)
    if not properties:
        return None
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
