"""Task reminder plugin — periodic system-reminder injection.

Mirrors upstream Claude Code's `task_reminder` attachment pattern
(`src/utils/attachments.ts:3395-3432` + `messages.ts:3680-3699`) via
ADK's `before_model_callback`.

Trigger: when both
  - assistant turns since the last `task_create`/`task_update` >=
    TURNS_SINCE_WRITE (default 10)
  - assistant turns since the last reminder >= TURNS_BETWEEN
    (default 10)

When triggered, reads the active task list from `TaskRunner.storage`
and appends a `<system-reminder>` block to
`llm_request.config.system_instruction`. The reminder text mirrors
upstream verbatim, with tool names rewritten to adk-cc snake_case.

Skipped for read-only specialists (Explore, verification) since they
don't manage tasks. Also skipped while the coordinator is in plan
mode — task tools are filtered out there.

Last-reminder tracking: stores the firing invocation_id in
`tool_context.state["task_reminder_last_invocation_id"]`. The next
turn's count walks events backward and stops at the matching
invocation.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from ..sandbox import get_workspace
from ..tasks import TaskStatus, get_runner

_SPECIALIST_AGENTS = frozenset({"Explore", "verification"})

_TASK_TOOL_NAMES = {"task_create", "task_update"}

_REMINDER_HEADER = (
    "The task tools haven't been used recently. If you're working on tasks "
    "that would benefit from tracking progress, consider using task_create "
    "to add new tasks and task_update to update task status (set to "
    "in_progress when starting, completed when done). Also consider "
    "cleaning up the task list if it has become stale. Only use these if "
    "relevant to the current work. This is just a gentle reminder - ignore "
    "if not applicable. Make sure that you NEVER mention this reminder to "
    "the user"
)

_LAST_REMINDER_KEY = "task_reminder_last_invocation_id"


def _has_function_call(event: Any, names: set[str]) -> bool:
    """True if event.content has a function_call whose name is in `names`."""
    content = getattr(event, "content", None)
    if content is None:
        return False
    parts = getattr(content, "parts", None) or []
    for p in parts:
        fc = getattr(p, "function_call", None)
        if fc is not None and getattr(fc, "name", None) in names:
            return True
    return False


def _is_thinking(event: Any) -> bool:
    """True if every part of event.content is a thinking-only part.

    ADK marks thinking parts via `Part.thought=True`. Skip these in
    turn counting (mirrors upstream's `isThinkingMessage` skip).
    """
    content = getattr(event, "content", None)
    if content is None:
        return False
    parts = getattr(content, "parts", None) or []
    if not parts:
        return False
    return all(getattr(p, "thought", False) for p in parts)


def _turns_since_task_call(events: Iterable[Any]) -> int:
    """Count assistant events back to the most recent task_create/task_update.

    Returns sys.maxsize when there's no prior task tool call.
    """
    count = 0
    for ev in reversed(list(events)):
        author = getattr(ev, "author", None)
        if not author or author == "user":
            continue
        if _is_thinking(ev):
            continue
        if _has_function_call(ev, _TASK_TOOL_NAMES):
            return count
        count += 1
    return sys.maxsize


def _turns_since_reminder(events: Iterable[Any], state: Any) -> int:
    """Count assistant events back to the last reminder firing.

    The reminder fires from before_model_callback (no event), so we
    track the firing invocation_id in state and walk events looking
    for that invocation_id.
    """
    try:
        last_inv = state.get(_LAST_REMINDER_KEY)
    except Exception:
        last_inv = None
    if not last_inv:
        return sys.maxsize
    count = 0
    for ev in reversed(list(events)):
        author = getattr(ev, "author", None)
        if not author or author == "user":
            continue
        if _is_thinking(ev):
            continue
        if getattr(ev, "invocation_id", None) == last_inv:
            return count
        count += 1
    return sys.maxsize


def _status_str(t: Any) -> str:
    s = getattr(t, "status", None)
    return s.value if hasattr(s, "value") else str(s)


def _render_reminder(tasks: list[Any], *, has_open: bool) -> str:
    """Render the reminder. When there are open (in_progress/pending)
    tasks, lead with an explicit close-them-out instruction and list
    them with ids the model can pass to task_update — that's the lever
    that actually drives completion, vs. the generic header alone."""
    if has_open:
        body = (
            "You have open tasks below. Before moving on or reporting "
            "completion: mark each finished task `completed` via "
            "task_update, and keep exactly one `in_progress` at a time. "
            "Only the items you've genuinely finished — don't close work "
            "that isn't done. Never mention this reminder to the user."
        )
    else:
        body = _REMINDER_HEADER
    if tasks:
        items = "\n".join(
            f"#{getattr(t, 'id', '?')[:8]}. [{_status_str(t)}] {getattr(t, 'title', '?')}"
            for t in tasks
        )
        body += f"\n\nHere are the existing tasks:\n\n{items}"
    return f"<system-reminder>\n{body}\n</system-reminder>"


def _append_to_system_instruction(req: LlmRequest, text: str) -> None:
    existing = req.config.system_instruction
    if existing is None:
        req.config.system_instruction = text
    elif isinstance(existing, str):
        req.config.system_instruction = existing + "\n\n" + text
    else:
        try:
            parts = list(existing) if isinstance(existing, list) else [existing]
            parts.append(types.Part(text=text))
            req.config.system_instruction = parts
        except Exception:
            pass


class TaskReminderPlugin(BasePlugin):
    """Injects the current task set into the model's context periodically."""

    def __init__(
        self,
        *,
        default_mode: str = "default",
        name: str = "adk_cc_task_reminder",
    ) -> None:
        super().__init__(name=name)
        # Fall back to the env-set default when state hasn't been
        # initialized — otherwise a session that boots with
        # `ADK_CC_PERMISSION_MODE=plan` would emit task reminders even
        # though task tools are filtered out by PlanModeReminderPlugin.
        self._default_mode = (default_mode or "default").lower()
        # Master on/off. `ADK_CC_TASK_REMINDER=0` disables the periodic
        # reminder injection entirely (the task TOOLS still work).
        self._enabled = os.environ.get("ADK_CC_TASK_REMINDER", "1") != "0"
        self._turns_since_write = int(
            os.environ.get("ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE", "10")
        )
        self._turns_between = int(
            os.environ.get("ADK_CC_TASK_REMINDER_TURNS_BETWEEN", "10")
        )
        # Completion-aware cadence: when an in_progress task is open, fire
        # after this many turns instead of `_turns_since_write` — the
        # "you left this open, close it" nudge. Set >= _turns_since_write
        # to disable the aggressive path and keep only the old cadence.
        self._open_turns = int(
            os.environ.get("ADK_CC_TASK_REMINDER_OPEN_TURNS", "3")
        )
        # Cooldown between reminders while an in_progress task is open.
        # Lower than `_turns_between` so the "close it out" nudge keeps
        # firing as the agent winds down — the regular 10-turn cooldown
        # silences it right when the agent is wrapping up with tasks
        # still open. Set >= _turns_between to disable.
        self._open_between = int(
            os.environ.get("ADK_CC_TASK_REMINDER_OPEN_BETWEEN", "2")
        )

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        if not self._enabled:
            return None
        agent_name = getattr(callback_context, "agent_name", None)
        if agent_name in _SPECIALIST_AGENTS:
            return None
        # Task tools are filtered out in plan mode; reminding the model
        # about tools it can't see just wastes context.
        try:
            mode = callback_context.state.get("permission_mode")
        except Exception:
            mode = None
        if (mode or self._default_mode) == "plan":
            return None

        events = list(getattr(callback_context.session, "events", []) or [])

        # A "fresh turn" is the opening model call after a new user
        # message — ADK appends the user message before invoking the
        # model, so events[-1].author == "user" identifies it. If the
        # previous turn ended with tasks still open, this is where we
        # catch it: the agent declared done mid-turn, and we can't force
        # closure after it already replied, but we CAN confront it the
        # moment the next turn starts. (This is the "dangling task"
        # check done without a persisted flag — robust for DB sessions
        # where temp/ad-hoc state wouldn't survive the invocation.)
        fresh_turn = bool(events) and getattr(events[-1], "author", None) == "user"

        turns_since = _turns_since_task_call(events)
        # Cheapest gate: on a non-fresh turn inside even the smallest
        # cadence window, bail before touching storage. A fresh turn
        # always checks (it might need to confront dangling tasks).
        if not fresh_turn and turns_since < min(
            self._open_turns, self._turns_since_write
        ):
            return None

        # Resolve the SAME (tenant, session) bucket the task tools wrote
        # to. get_workspace returns the seeded WorkspaceRoot in prod and
        # the local/local default in dev — matching task_create either
        # way. (The old code read the dataclass as a dict and always got
        # local/local, so prod reminders showed an empty list.)
        try:
            ws = get_workspace(callback_context)
            runner = get_runner()
            all_tasks = await runner.storage.list(
                tenant_id=ws.tenant_id or "local",
                session_id=ws.session_id or "local",
            )
        except Exception:
            all_tasks = []

        # Open = anything not completed; in_progress drives the
        # aggressive cadence + shorter cooldown.
        open_tasks = [
            t for t in all_tasks
            if getattr(t, "status", None) != TaskStatus.COMPLETED
        ]
        has_in_progress = any(
            getattr(t, "status", None) == TaskStatus.IN_PROGRESS
            for t in all_tasks
        )

        since_reminder = _turns_since_reminder(events, callback_context.state)

        # Dangling-task confrontation: a new turn that opens with tasks
        # still open fires immediately, bypassing the cooldown — this is
        # what actually catches "declared done last turn with open tasks."
        fire = fresh_turn and bool(open_tasks)

        if not fire:
            # Periodic / completion-aware cadence. in_progress shortens
            # both the trigger threshold and the cooldown so the nudge
            # reaches the model mid-task and again as it winds down,
            # rather than only after 10 idle turns.
            threshold = self._open_turns if has_in_progress else self._turns_since_write
            cooldown = self._open_between if has_in_progress else self._turns_between
            if turns_since >= threshold and since_reminder >= cooldown:
                fire = True

        if not fire:
            return None

        _append_to_system_instruction(
            llm_request,
            _render_reminder(open_tasks, has_open=bool(open_tasks)),
        )
        try:
            callback_context.state[_LAST_REMINDER_KEY] = (
                getattr(callback_context, "invocation_id", None)
            )
        except Exception:
            pass
        return None
