"""Stage-guard plugin — nudges and (selectively) enforces the
explore → plan → act → verify loop on the coordinator.

How the loop is tracked
-----------------------

`state["temp:loop_stage"]` carries the current stage as one of:

    "explore" | "plan" | "act" | "verify" | "done"

The plugin uses three signals to decide stage transitions and the
nudge content:

  - **Tool stage tags.** Every tool exposed by the data-science agent
    sets `cls.stage: ClassVar[str]`. `before_tool_callback` reads it
    to know which loop phase a call belongs to.
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
acceptable next. The nudges accumulate AS THE STAGE CHANGES — they
don't force any specific tool, just remind the model of the rule.

Enforcement (hard, `before_tool_callback`)
------------------------------------------

Two rules are STRICTLY enforced (returning a non-None tool result so
ADK skips the actual tool invocation):

  1. `verify_completion` cannot run before a plan has been recorded
     AND every plan step is `status=done`. The verifier itself
     redundantly rule-checks these — but the guard refuses to invoke
     it at all, so the model gets a clearer "you skipped the loop"
     signal in the audit trail.
  2. Acting tools cannot run before a plan is recorded. This catches
     the common LLM mistake of jumping straight to `aggregate_dataset`
     after a single `describe_dataset`. Explore tools always work;
     the agent can revisit explore at any time.

Stage transitions on `after_tool_callback`
------------------------------------------

The transition table is intentionally small:

    (any tool, stage=None)        → "explore"
    second explore tool (stage was already "explore")
                                  → "plan"
    record_plan                   → "act"
    mark_step_done that finishes the plan
                                  → "verify"
    verify_completion (verdict=PASS)
                                  → "done"

The plugin never moves the stage backward — if the agent re-enters
explore mid-loop (e.g. to check a value), the stage tag stays where
it was. The nudge text simply notes that re-exploration is fine.
"""

from __future__ import annotations

import logging
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

_log = logging.getLogger(__name__)

_STAGE_KEY = "temp:loop_stage"
_PLAN_KEY = "temp:loop_plan"
_RESULTS_KEY = "temp:loop_results"

_STAGES = ("explore", "plan", "act", "verify", "done")

# Specialist sub-agent names by stage. Used by the transfer-gate inside
# `before_tool_callback` to refuse transfers into ACT-stage specialists
# until a plan has been recorded.
_EXPLORE_SPECIALISTS = frozenset({"loader", "explorer"})
_ACT_SPECIALISTS = frozenset({"processor", "visualizer"})


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
        "transferring to `processor` or `visualizer`. After each "
        "specialist returns, call `mark_step_done(step_index, evidence)` "
        "with a short evidence string. When every step is marked done, "
        "proceed to verify."
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

    # --- enforce ------------------------------------------------------

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        stage_tag = getattr(tool, "stage", None) or getattr(
            type(tool), "stage", None
        )
        plan = _safe_state(tool_context, _PLAN_KEY) or []
        plan_recorded = bool(plan)
        all_done = plan_recorded and all(p.get("status") == "done" for p in plan)

        if tool.name == "verify_completion":
            if not plan_recorded:
                self._emit_block(tool_context, tool.name, "no plan recorded")
                return {
                    "status": "stage_violation",
                    "rule": "verify_completion needs a recorded plan",
                    "hint": "Call record_plan(steps=[...]) first.",
                }
            if not all_done:
                pending = [p["step"] for p in plan if p.get("status") != "done"]
                self._emit_block(tool_context, tool.name, "plan not complete")
                return {
                    "status": "stage_violation",
                    "rule": "verify_completion needs every plan step status=done",
                    "pending_steps": pending,
                    "hint": "Finish the remaining steps and mark_step_done before verifying.",
                }

        if stage_tag == "act" and not plan_recorded and tool.name != "mark_step_done":
            self._emit_block(tool_context, tool.name, "act before plan")
            return {
                "status": "stage_violation",
                "rule": "acting tools require a recorded plan",
                "hint": (
                    "Explore the datasets and then call record_plan(steps=[...]) "
                    "before invoking any acting tool."
                ),
            }

        # Gate transfers to ACT-stage specialists (processor, visualizer)
        # the same way we gate direct acting tools. Transfers to
        # EXPLORE-stage specialists (loader, explorer) are always allowed.
        if tool.name == "transfer_to_agent":
            target = (tool_args or {}).get("agent_name")
            if target in _ACT_SPECIALISTS and not plan_recorded:
                self._emit_block(tool_context, tool.name, f"transfer to {target} before plan")
                return {
                    "status": "stage_violation",
                    "rule": f"transfer to {target!r} requires a recorded plan",
                    "hint": (
                        "Finish exploring, then call record_plan(steps=[...]) on "
                        "the coordinator before dispatching to an acting specialist."
                    ),
                }

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
        elif tool.name == "mark_step_done":
            plan = _safe_state(tool_context, _PLAN_KEY) or []
            if plan and all(p.get("status") == "done" for p in plan):
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

    # --- audit helper -------------------------------------------------

    def _emit_block(self, ctx: ToolContext, tool_name: str, reason: str) -> None:
        if not is_audit_enabled():
            return
        emit_audit_event(
            {
                "ts": time.time(),
                "event": "loop_stage_block",
                "tool_name": tool_name,
                "reason": reason,
            }
        )


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
