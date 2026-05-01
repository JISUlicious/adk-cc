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

Skipped for read-only specialists (Plan, Explore, verification) since
they don't manage tasks.

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

from ..tasks import TaskStatus, get_runner

_SPECIALIST_AGENTS = frozenset({"Plan", "Explore", "verification"})

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


def _render_reminder(tasks: list[Any]) -> str:
    body = _REMINDER_HEADER
    if tasks:
        items = "\n".join(
            f"#{getattr(t, 'id', '?')[:8]}. [{getattr(t, 'status', '?').value if hasattr(getattr(t, 'status', None), 'value') else getattr(t, 'status', '?')}] {getattr(t, 'title', '?')}"
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

    def __init__(self, name: str = "adk_cc_task_reminder") -> None:
        super().__init__(name=name)
        self._turns_since_write = int(
            os.environ.get("ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE", "10")
        )
        self._turns_between = int(
            os.environ.get("ADK_CC_TASK_REMINDER_TURNS_BETWEEN", "10")
        )

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        agent_name = getattr(callback_context, "agent_name", None)
        if agent_name in _SPECIALIST_AGENTS:
            return None

        events = list(getattr(callback_context.session, "events", []) or [])

        if _turns_since_task_call(events) < self._turns_since_write:
            return None
        if _turns_since_reminder(events, callback_context.state) < self._turns_between:
            return None

        # Pull active tasks for this session. Best-effort — never let a
        # storage hiccup tank the request.
        try:
            ws = callback_context.state.get("sandbox_workspace") or {}
            tenant_id = ws.get("tenant_id") if isinstance(ws, dict) else "local"
            session_id = ws.get("session_id") if isinstance(ws, dict) else "local"
            runner = get_runner()
            tasks = await runner.storage.list(
                tenant_id=tenant_id or "local",
                session_id=session_id or "local",
            )
            # Only show non-terminal tasks (most actionable).
            tasks = [
                t for t in tasks
                if getattr(t, "status", None) not in (TaskStatus.COMPLETED,)
            ]
        except Exception:
            tasks = []

        _append_to_system_instruction(llm_request, _render_reminder(tasks))
        try:
            callback_context.state[_LAST_REMINDER_KEY] = (
                getattr(callback_context, "invocation_id", None)
            )
        except Exception:
            pass
        return None
