"""Render `ask_user_question` as N checkboxes per question + an optional
free-form text input — instead of forcing the operator to type each
answer verbatim.

Why this exists
---------------

`ask_user_question`'s tool input is `{questions: [{question, options,
multi_select, ...}]}`. The bundled `adk web` UI renders long-running
tool calls via its `app-long-running-response` widget, which (per
`google/adk/cli/browser/main-*.js`) reads JSON Schema **`type`** only:

  - `boolean` → checkbox input
  - `integer`/`number` → numeric input
  - else (incl. `string`) → free-form text input

`enum` is **not consulted** — so a `{type: "string", enum: [labels]}`
schema renders as a plain textbox where the operator has to type one
of the option labels exactly, and a typo silently fails.

This plugin does two rewrites so the bundled UI surfaces a proper
select-from-options experience:

**Outbound** (`after_model_callback`):
  Replace the per-question `{type: string, enum: [...]}` schema with
  one boolean property per option, plus one optional free-form
  `q{i}_other` string per question. Bundled UI renders each option as
  a checkbox and the free-form as a one-line textbox.
  Property keys are positional (`q{i}_opt{j}`, `q{i}_other`) so the
  inbound rewrite can map answers back to their source question/option.

**Inbound** (`on_user_message_callback`):
  When the operator submits the form, the response is
  `{q0_opt0: true, q0_opt1: false, q0_other: "...", q1_opt0: true,
  ...}`. The plugin scans session events to recover the original
  `questions` list (positional lookup by call id), then reshapes the
  response back to the natural `{<question text>: <answer>}` shape
  the tool's docstring promises. The LLM sees the clean shape on
  resume.

Selection semantics:
  - `multi_select=False` (radio): take the first true-valued option,
    unless `q{i}_other` is non-empty in which case the typed text wins.
    Bundled UI can't physically prevent the operator from ticking
    multiple, but the description on each option says "Tick to choose
    (only one)" and we deterministically pick the first.
  - `multi_select=True` (checkbox): include every true-valued option's
    label. If `q{i}_other` is non-empty, append it as an additional
    label.
  - No tick + no text: answer is empty string.

The injected schema is invisible to the tool itself — `_execute` reads
the validated `AskUserQuestionArgs`, not the raw args dict, so neither
`response_schema` nor the per-option booleans show up in the tool's
view of args.

Disabling this plugin reverts to the bundled UI's free-form textarea
fallback — the tool itself keeps working; only the rendering changes.
"""
from __future__ import annotations

from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types


ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"


class AskUserQuestionUiHintPlugin(BasePlugin):
    """Outbound: inject boolean-per-option schema for the bundled UI.
    Inbound: reshape the form-widget output back to the natural
    `{question: <answer>}` shape the tool docstring promises.
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
            if fc is None or fc.name != ASK_USER_QUESTION_TOOL_NAME:
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

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> Optional[types.Content]:
        if not user_message.parts:
            return None
        mutated = False
        new_parts: list[types.Part] = []
        for part in user_message.parts:
            fr = getattr(part, "function_response", None)
            if fr is None or fr.name != ASK_USER_QUESTION_TOOL_NAME:
                new_parts.append(part)
                continue
            questions = _find_questions_for_call(invocation_context, fr.id)
            if not questions:
                # No matching call in session — leave the response alone
                # so any error surfaces naturally rather than being
                # silently misinterpreted.
                new_parts.append(part)
                continue
            response = fr.response if isinstance(fr.response, dict) else {}
            # If the response is ALREADY in the natural shape (a custom
            # frontend submitted a clean `{question: answer}` map), pass
            # through untouched. The bundled UI's form submission has
            # `q{i}_opt{j}` keys that the natural shape never uses.
            if not _looks_like_form_widget_response(response):
                new_parts.append(part)
                continue
            answers = _reshape_answers(response, questions)
            new_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=fr.id,
                        name=fr.name,
                        response={"status": "answered", "answers": answers},
                    )
                )
            )
            mutated = True
        if not mutated:
            return None
        return types.Content(role=user_message.role, parts=new_parts)


# --- Schema construction (outbound) --------------------------------


def _build_response_schema(questions: list[Any]) -> Optional[dict]:
    """Per-option boolean checkboxes + an optional free-form text field
    per question. See module docstring for the rendering rationale.

    Returns None when there's nothing useful to expose (all questions
    malformed). One question with zero options still gets a free-form
    text field — preserves the "ask anything" path."""
    properties: dict[str, Any] = {}
    for i, q in enumerate(questions):
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
        multi_select = bool(q.get("multi_select"))
        pick_hint = "any that apply" if multi_select else "only one"

        # Per-option checkboxes. Description includes the question + the
        # label so the bundled UI's flat property list reads sensibly.
        for j, label in enumerate(labels):
            key = f"q{i}_opt{j}"
            opt_desc = ""
            for o in options:
                if isinstance(o, dict) and o.get("label") == label:
                    opt_desc = o.get("description") or ""
                    break
            tail = f"{label} — {opt_desc}" if opt_desc else label
            properties[key] = {
                "type": "boolean",
                "description": f"[{qtext}] Pick {pick_hint}: {tail}",
            }

        # Optional free-form text. Always included so the operator can
        # type a custom answer alongside (or instead of) ticking.
        properties[f"q{i}_other"] = {
            "type": "string",
            "description": (
                f"[{qtext}] Other / free-form (leave blank to use the "
                f"checkbox(es) above)"
            ),
        }

    if not properties:
        return None
    return {"type": "object", "properties": properties}


# --- Response reshape (inbound) ------------------------------------


def _find_questions_for_call(
    invocation_context: Optional[InvocationContext], call_id: Optional[str]
) -> list[dict]:
    """Recover the questions list from the function-call that triggered
    this response. Scans session events for the function-call with
    matching id and returns its `args.questions`. Empty list when the
    call can't be located (defensive)."""
    if not call_id or invocation_context is None:
        return []
    session = getattr(invocation_context, "session", None)
    if session is None:
        return []
    events = getattr(session, "events", None)
    if not events:
        return []
    for ev in events:
        for fc in ev.get_function_calls():
            if fc.name != ASK_USER_QUESTION_TOOL_NAME:
                continue
            if fc.id != call_id:
                continue
            args = fc.args or {}
            questions = args.get("questions")
            if isinstance(questions, list):
                return questions
    return []


def _looks_like_form_widget_response(response: dict) -> bool:
    """Heuristic: bundled UI form widget submits keys shaped like
    `q<int>_opt<int>` or `q<int>_other`. The natural answer shape uses
    question text as keys, which never have that pattern. Cheap check
    so a custom payload-aware frontend can submit `{question: answer}`
    directly and skip the reshape."""
    if not response:
        return False
    for key in response:
        if not isinstance(key, str):
            return False
        if not (key.startswith("q") and ("_opt" in key or key.endswith("_other"))):
            return False
    return True


def _reshape_answers(response: dict, questions: list[dict]) -> dict:
    """Convert form-widget submission (`{q0_opt0: bool, q0_other: str, ...}`)
    back to natural shape (`{<question text>: <answer>}`). See module
    docstring for selection semantics."""
    answers: dict[str, Any] = {}
    for i, q in enumerate(questions):
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
        multi_select = bool(q.get("multi_select"))

        ticked: list[str] = []
        for j, label in enumerate(labels):
            if response.get(f"q{i}_opt{j}") is True:
                ticked.append(label)

        other_raw = response.get(f"q{i}_other")
        other = (
            other_raw.strip() if isinstance(other_raw, str) else ""
        )

        if multi_select:
            answer: Any = list(ticked)
            if other:
                answer.append(other)
            answers[qtext] = answer
        else:
            # Free-form text overrides the checkbox pick when both are
            # present; otherwise take the first ticked label.
            if other:
                answers[qtext] = other
            elif ticked:
                answers[qtext] = ticked[0]
            else:
                answers[qtext] = ""
    return answers
