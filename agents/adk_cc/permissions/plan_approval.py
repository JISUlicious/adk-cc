"""The plan-approval prompt, shared by `write_plan` and `exit_plan_mode`.

One definition, because the two paths must offer the SAME choices — a user who
approves from the write_plan card and a user who approves from exit_plan_mode
are doing the same thing, and any drift between the two option sets would show
up as "why does this dialog have a Revise button and that one doesn't".

Three options, not two. Approve and Deny leave no way to say "close, but change
this": denying to ask for a change reads as rejection to the user and tells the
model to stop rather than iterate, while the comment box alone cannot express
which of the two you meant. **Revise** is the third state — feedback without a
verdict, plan mode continues, the model comes back with an updated plan.

The confirmation layer treats `confirmed = chose_id != "deny"`, so `revise`
arrives looking like an approval and MUST be intercepted by the tool before it
acts. Both tools do that via `chose_id()`.
"""

from __future__ import annotations

from typing import Any, Optional

from .confirmation import ConfirmOption, ConfirmPrompt

REVISE = "revise"
APPROVE = "approve"
DENY = "deny"

REVISION_MESSAGE = (
    "The user wants the plan revised, NOT rejected and not approved. Stay in "
    "plan mode: incorporate their comment, then ask for approval again with "
    "the updated plan. Do not start implementing."
)


def plan_confirm_prompt(
    *, title: str, detail: str, approve_description: str
) -> ConfirmPrompt:
    """Approve / Revise / Deny, with a comment box."""
    return ConfirmPrompt(
        style="single_select",
        title=title,
        detail=detail,
        options=[
            ConfirmOption(
                id=APPROVE,
                label="Approve",
                description=approve_description,
            ),
            ConfirmOption(
                id=REVISE,
                label="Revise",
                description=(
                    "Keep planning. Your comment goes back to the model, which "
                    "updates the plan and asks again — neither an approval nor "
                    "a rejection."
                ),
            ),
            ConfirmOption(
                id=DENY,
                label="Deny",
                description=(
                    "Stop. Stay in plan mode and do not re-propose until asked; "
                    "the model sees your comment (if any)."
                ),
            ),
        ],
        with_comment=True,
    )


def chose_id(ctx: Any) -> str:
    """Which option the user picked, or "" when there is no confirmation."""
    payload = getattr(getattr(ctx, "tool_confirmation", None), "payload", None)
    return (payload.get("chose_id") or "") if isinstance(payload, dict) else ""


def revision_response(
    *, plan_summary: str, comment: Optional[str] = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "revision_requested",
        "current_mode": "plan",
        "message": REVISION_MESSAGE,
        "plan_summary": plan_summary,
    }
    if comment:
        out["user_comment"] = comment
    return out


def exit_plan_mode_state(ctx: Any, *, default_mode: str = "default") -> dict[str, Any]:
    """Flip the session out of plan mode. Shared so approving from `write_plan`
    and from `exit_plan_mode` do EXACTLY the same thing — the user asked why one
    would need a second tool call to finish what the first already decided.

    Returns `{"status": "noop"| "approved", ...}`; the caller merges its own
    fields in. Restores the mode recorded by `enter_plan_mode` rather than a
    hardcoded default, never restores INTO plan mode (a stale marker would trap
    the session), and consumes the marker so it applies to one cycle only.
    """
    try:
        previous = ctx.state.get("permission_mode")
    except Exception:  # noqa: BLE001
        previous = None
    if not previous:
        previous = (default_mode or "default").lower()
    if previous != "plan":
        return {
            "status": "noop",
            "current_mode": previous,
            "message": (
                f"Not in plan mode (current: {previous!r}); nothing to exit. "
                "Use the regular tools to proceed."
            ),
        }

    try:
        restored = ctx.state.get("plan_previous_mode")
    except Exception:  # noqa: BLE001
        restored = None
    if not restored or restored == "plan":
        restored = "default"
    try:
        ctx.state["permission_mode"] = restored
        ctx.state["plan_previous_mode"] = None      # consumed — one cycle only
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"could not update state: {e}"}

    from ..plugins.audit import emit_state_mutation  # deferred: import cycle

    emit_state_mutation(
        mutation_type="permission_mode_change",
        state_key="permission_mode",
        details={"previous_value": previous, "new_value": restored},
        ctx=ctx,
    )
    return {"status": "approved", "previous_mode": previous, "new_mode": restored}
