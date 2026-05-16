"""Stage-guard plugin — nudges the coordinator through the
explore → plan → act → verify loop. Soft nudge only; no hard gates.

How the loop is tracked
-----------------------

`state["temp:loop_stage"]` carries the current stage as one of:

    "explore" | "plan" | "act" | "verify" | "done"

The plugin uses three signals to decide stage transitions and the
nudge content:

  - **Tool stage tags.** Every tool exposed by the data-science agent
    sets `cls.stage: ClassVar[str]`. `after_tool_callback` reads it
    to decide whether the just-fired call should advance the stage.
  - **Plan presence + completion.** `state["temp:loop_plan"]` is a
    list of `{step, status, evidence}` dicts written by
    `record_plan` and updated by `mark_step_done`.
  - **Acting-tool results.** `state["temp:loop_results"]` is appended
    to by each acting tool — its length tells us how much work has
    actually happened.

Nudges (soft, `before_model_callback`)
--------------------------------------

A short `<stage-nudge>` block is prepended to `system_instruction`
each turn, telling the model where in the loop we are and what's
acceptable next. Pure advisory — nothing is blocked.

The contract that REAL enforcement happens IS still meaningful, but
it lives inside `verify_completion` itself — that tool's rule check
(plan recorded, every step status=done with evidence, ≥1 acting
result, conclusion non-empty) plus the LLM judgment decide PASS /
FAIL. If the model tries to verify prematurely, the verifier
returns FAIL and the loop continues; we don't refuse the call.

Stage transitions on `after_tool_callback`
------------------------------------------

The transition table is intentionally small:

    (any tool, stage=None)        → "explore"
    second explore tool (stage was already "explore")
                                  → "plan"
    record_plan                   → "act"
    acting-tool result, and
      len(temp:loop_results) >= len(temp:loop_plan)
                                  → "verify"
    verify_completion (verdict=PASS)
                                  → "done"

The act → verify rule is the post-redesign shape: there's no
`mark_step_done` tool anymore; the specialist's handback is the
step-done signal, and the framework infers completion from result
count. One acting-tool result per plan step is the contract.

The plugin never moves the stage backward — if the agent re-enters
explore mid-loop (e.g. to check a value), the stage tag stays where
it was. The nudge text simply notes that re-exploration is fine.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .audit import emit_audit_event, is_audit_enabled

_STAGE_KEY = "temp:loop_stage"
_PLAN_KEY = "temp:loop_plan"
_RESULTS_KEY = "temp:loop_results"


_NUDGE_BY_STAGE = {
    None: (
        "You are at the START of the explore→plan→act→verify loop. "
        "First action MUST be a transfer to `loader` or `explorer`, or "
        "a coordinator-side read of state — gather context before "
        "planning anything."
    ),
    "explore": (
        "You are in EXPLORE. Use the loader / explorer specialists "
        "until you have enough context to plan. When ready, call "
        "`record_plan(steps=[...])` directly — no separate reasoning "
        "text is required."
    ),
    "plan": (
        "You have explored. Your next action MUST be "
        "`record_plan(steps=[...])` with the ordered computations you "
        "intend to run. Do NOT transfer to acting specialists "
        "(processor / visualizer) until the plan is recorded."
    ),
    "act": (
        "You are in ACT. Execute the plan one step at a time by "
        "transferring to `processor` or `visualizer`. The framework "
        "infers step completion from the specialist's handback — you "
        "do NOT need to mark steps done. When the count of acting-tool "
        "results reaches the plan length, the stage advances to verify "
        "automatically and you should call `verify_completion`."
    ),
    "verify": (
        "Every plan step is done. Call `verify_completion(user_query, "
        "conclusion, llm_judgment)` with your draft answer and a "
        "self-assessment of whether it satisfies the original query. "
        "Do NOT emit the user-facing reply until verify returns PASS."
    ),
    "done": (
        "Verification PASSED. Emit the final user-facing reply as plain "
        "text in your next response — no more tool calls."
    ),
}


class StageGuardPlugin(BasePlugin):
    """Pushes the coordinator through the gather/plan/act/verify loop."""

    def __init__(self, name: str = "adk_cc_stage_guard") -> None:
        super().__init__(name=name)

    # --- nudge --------------------------------------------------------

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        try:
            stage = callback_context.state.get(_STAGE_KEY)
        except Exception:
            stage = None
        text = _NUDGE_BY_STAGE.get(stage) or _NUDGE_BY_STAGE[None]
        block = f"<stage-nudge stage={stage or 'start'}>\n{text}\n</stage-nudge>"
        _prepend_to_system_instruction(llm_request, block)
        return None

    # --- advance ------------------------------------------------------

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: Any,
    ) -> Optional[dict]:
        stage_tag = getattr(tool, "stage", None) or getattr(
            type(tool), "stage", None
        )
        try:
            current = tool_context.state.get(_STAGE_KEY)
        except Exception:
            current = None

        new_stage = current
        if stage_tag == "explore":
            if current is None:
                new_stage = "explore"
            elif current == "explore":
                # Second explore tool — advance the nudge to PLAN so the
                # model knows the next move is `record_plan`. The plan
                # stage itself is short-lived; calling `record_plan`
                # advances to ACT below.
                new_stage = "plan"
        elif tool.name == "record_plan":
            new_stage = "act"
        elif stage_tag == "act":
            # Acting-tool result. Step completion is inferred from the
            # result count reaching the plan length — there's no
            # `mark_step_done` tool anymore (the specialist's handback
            # IS the step-done signal).
            plan = _safe_state(tool_context, _PLAN_KEY) or []
            results = _safe_state(tool_context, _RESULTS_KEY) or []
            if plan and len(results) >= len(plan):
                new_stage = "verify"
        elif tool.name == "verify_completion":
            if isinstance(result, dict) and result.get("verdict") == "PASS":
                new_stage = "done"

        if new_stage != current:
            try:
                tool_context.state[_STAGE_KEY] = new_stage
            except Exception:
                pass
            if is_audit_enabled():
                emit_audit_event(
                    {
                        "ts": time.time(),
                        "event": "loop_stage_transition",
                        "from": current,
                        "to": new_stage,
                        "trigger_tool": tool.name,
                    }
                )
        return None


# --- helpers --------------------------------------------------------


def _safe_state(ctx: Any, key: str) -> Optional[Any]:
    try:
        return ctx.state.get(key)
    except Exception:
        return None


def _prepend_to_system_instruction(req: LlmRequest, text: str) -> None:
    """Same shape as ProjectContextPlugin's helper. Prepends `text`
    onto req.config.system_instruction, handling None / str / Part /
    list[Part] uniformly."""
    cfg = req.config
    existing = getattr(cfg, "system_instruction", None)
    if existing is None:
        cfg.system_instruction = text
    elif isinstance(existing, str):
        cfg.system_instruction = text + "\n\n" + existing
    else:
        try:
            parts = (
                list(existing) if isinstance(existing, list) else [existing]
            )
            parts.insert(0, types.Part(text=text))
            cfg.system_instruction = parts
        except Exception:
            pass
