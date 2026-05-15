"""Multi-step data-filter workflow on a bare-agent + plugin chain.

Wires five plain-ADK `FunctionTool`s — no `adk_cc.tools.*` dependencies
— and drives a scripted LLM through a 5-step filter/sort/summarize
pipeline. Proves the bare-agent chassis can host arbitrary tool
surfaces and that the audit trail captures every step in order.

Tools (state-threaded via `tool_context.state['employees']`)
-----------------------------------------------------------

  1. `load_employees()`               — seeds state with a 6-row dataset
  2. `filter_by_department(dept)`     — keeps rows matching dept
  3. `filter_by_min_salary(min)`      — keeps rows >= min salary
  4. `sort_by_salary(descending)`     — sorts in place
  5. `summarize_salary(operation)`    — returns count/avg/min/max

Workflow scenario
-----------------

User asks: "Find the average salary of engineering employees earning
at least $90k, sorted from highest to lowest."

The scripted LLM emits the five tool calls in sequence; after each,
ADK feeds the tool result back so the model has fresh state for the
next call. The final response is a plain-text summary.

Run
---

`.venv/bin/python examples/data_workflow.py`

Prints the per-step state, the audit JSONL trail, and the final
text reply.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

_TMP = Path(tempfile.mkdtemp(prefix="data_workflow_demo_"))
_AUDIT_PATH = _TMP / "audit.jsonl"
_PROJECT_DIR = _TMP / "project"
_PROJECT_DIR.mkdir()
(_PROJECT_DIR / "CLAUDE.md").write_text(
    "# Project conventions\n\n"
    "- Data-workflow demo project.\n"
    "- Prefer tool chains over inlined logic.\n"
)

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-demo")
os.environ["ADK_CC_LOG_LEVEL"] = "INFO"
os.environ["ADK_CC_AUDIT_LOG"] = str(_AUDIT_PATH)
os.environ["ADK_CC_LOG_MODEL_IO"] = "1"

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import Field

from adk_cc.logging_setup import configure_logging
configure_logging()

from adk_cc.plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    ModelIOTracePlugin,
    PermissionPlugin,
    ProjectContextPlugin,
)
from adk_cc.permissions import SettingsHierarchy


# --- Dataset -------------------------------------------------------

_EMPLOYEES: list[dict[str, Any]] = [
    {"name": "Alice",  "dept": "eng",     "salary": 120_000},
    {"name": "Bob",    "dept": "eng",     "salary":  85_000},
    {"name": "Carol",  "dept": "sales",   "salary": 110_000},
    {"name": "Dave",   "dept": "eng",     "salary":  95_000},
    {"name": "Eve",    "dept": "sales",   "salary": 130_000},
    {"name": "Frank",  "dept": "eng",     "salary": 105_000},
]


# --- Tool implementations -----------------------------------------
#
# State key: `temp:employees` (the `temp:` prefix keeps these out of
# ADK's persisted session-state delta, matching the convention used
# elsewhere in adk-cc for per-turn scratch).

_STATE_KEY = "temp:employees"


def load_employees(tool_context: ToolContext) -> dict[str, Any]:
    """Seed session state with the hardcoded employee dataset.

    Returns the row count so the model can confirm load.
    """
    tool_context.state[_STATE_KEY] = list(_EMPLOYEES)
    return {"status": "ok", "rows_loaded": len(_EMPLOYEES)}


def filter_by_department(
    department: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Keep only rows where `dept == department`.

    Returns the count of rows kept and the count dropped.
    """
    rows = tool_context.state.get(_STATE_KEY) or []
    before = len(rows)
    kept = [r for r in rows if r.get("dept") == department]
    tool_context.state[_STATE_KEY] = kept
    return {
        "status": "ok",
        "department": department,
        "rows_in": before,
        "rows_kept": len(kept),
        "rows_dropped": before - len(kept),
    }


def filter_by_min_salary(
    min_salary: int, tool_context: ToolContext
) -> dict[str, Any]:
    """Keep only rows where `salary >= min_salary`."""
    rows = tool_context.state.get(_STATE_KEY) or []
    before = len(rows)
    kept = [r for r in rows if r.get("salary", 0) >= min_salary]
    tool_context.state[_STATE_KEY] = kept
    return {
        "status": "ok",
        "min_salary": min_salary,
        "rows_in": before,
        "rows_kept": len(kept),
        "rows_dropped": before - len(kept),
    }


def sort_by_salary(
    descending: bool, tool_context: ToolContext
) -> dict[str, Any]:
    """Sort the current dataset by salary."""
    rows = tool_context.state.get(_STATE_KEY) or []
    rows_sorted = sorted(rows, key=lambda r: r.get("salary", 0), reverse=descending)
    tool_context.state[_STATE_KEY] = rows_sorted
    return {
        "status": "ok",
        "descending": descending,
        "rows": [
            {"name": r["name"], "salary": r["salary"]} for r in rows_sorted
        ],
    }


def summarize_salary(
    operation: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Aggregate salary across the current dataset.

    `operation` is one of: count, avg, min, max, sum.
    """
    rows = tool_context.state.get(_STATE_KEY) or []
    salaries = [r.get("salary", 0) for r in rows]
    if operation == "count":
        value: float = len(salaries)
    elif operation == "sum":
        value = sum(salaries)
    elif operation == "avg":
        value = (sum(salaries) / len(salaries)) if salaries else 0
    elif operation == "min":
        value = min(salaries) if salaries else 0
    elif operation == "max":
        value = max(salaries) if salaries else 0
    else:
        return {
            "status": "error",
            "error": f"unknown operation {operation!r}",
        }
    return {
        "status": "ok",
        "operation": operation,
        "rows_in": len(salaries),
        "value": value,
    }


# --- Scripted LLM driving the workflow ----------------------------


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> LlmResponse:
    """LlmResponse carrying a single function_call part."""
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=call_id, name=name, args=args
                    )
                )
            ],
        ),
        partial=False,
    )


def _text(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=text)]
        ),
        partial=False,
    )


_WORKFLOW_RESPONSES: list[LlmResponse] = [
    # Step 1 — load the dataset.
    _tool_call("step-1-load", "load_employees", {}),
    # Step 2 — narrow to engineering.
    _tool_call(
        "step-2-dept",
        "filter_by_department",
        {"department": "eng"},
    ),
    # Step 3 — narrow further by salary floor.
    _tool_call(
        "step-3-salary",
        "filter_by_min_salary",
        {"min_salary": 90_000},
    ),
    # Step 4 — order by salary descending.
    _tool_call(
        "step-4-sort",
        "sort_by_salary",
        {"descending": True},
    ),
    # Step 5 — aggregate.
    _tool_call(
        "step-5-summary",
        "summarize_salary",
        {"operation": "avg"},
    ),
    # Final reply.
    _text(
        "Engineers earning at least $90k (highest first): Alice ($120k), "
        "Frank ($105k), Dave ($95k). Average salary: $106,666.67."
    ),
]


class _ScriptedLlm(BaseLlm):
    """Replays the queued responses one per `generate_content_async`."""

    model: str = "fake/data-workflow"
    responses: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if not self.responses:
            raise RuntimeError("_ScriptedLlm queue empty")
        yield self.responses.pop(0)


# --- Assembly -----------------------------------------------------


def _build_tools() -> list[FunctionTool]:
    return [
        FunctionTool(load_employees),
        FunctionTool(filter_by_department),
        FunctionTool(filter_by_min_salary),
        FunctionTool(sort_by_salary),
        FunctionTool(summarize_salary),
    ]


def _build_agent() -> LlmAgent:
    return LlmAgent(
        name="data_workflow_agent",
        model=_ScriptedLlm(responses=list(_WORKFLOW_RESPONSES)),
        instruction=(
            "You are a data assistant. Use the available filter / sort / "
            "summarize tools to answer the user's question. Each tool "
            "mutates session state; call them in order."
        ),
        tools=_build_tools(),
    )


def _build_plugin_chain() -> list:
    return [
        AuditPlugin(),
        PermissionPlugin(SettingsHierarchy.empty()),
        ProjectContextPlugin(),
        ContextGuardPlugin(),
        ModelIOTracePlugin(),
    ]


async def run() -> int:
    prev_cwd = os.getcwd()
    os.chdir(_PROJECT_DIR)
    try:
        runner = InMemoryRunner(
            agent=_build_agent(),
            plugins=_build_plugin_chain(),
            app_name="adk_cc_data_workflow",
        )
        user_id = "alice"
        session_id = f"workflow-{uuid.uuid4().hex[:8]}"
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        print(f"[workflow] project dir: {_PROJECT_DIR}")
        print(f"[workflow] audit log:   {_AUDIT_PATH}")
        print(f"[workflow] session_id:  {session_id}")
        print("[workflow] user prompt:  "
              "'Find the average salary of engineering employees earning "
              "at least $90k, sorted from highest to lowest.'\n")
        print("[workflow] driving scripted workflow...")
        final_text_parts: list[str] = []
        async for ev in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            "Find the average salary of engineering "
                            "employees earning at least $90k, sorted "
                            "from highest to lowest."
                        )
                    )
                ],
            ),
        ):
            # Collect final-turn text replies for printing.
            if (
                getattr(ev, "author", None) == "data_workflow_agent"
                and getattr(ev, "content", None) is not None
            ):
                for part in (ev.content.parts or []):
                    if getattr(part, "text", None):
                        final_text_parts.append(part.text)
        print("[workflow] turn done.\n")
        if final_text_parts:
            print("--- FINAL MODEL TEXT ---")
            for t in final_text_parts:
                print(f"  {t}")
            print()
    finally:
        os.chdir(prev_cwd)

    if not _AUDIT_PATH.exists():
        print(f"[workflow] WARN: no audit log at {_AUDIT_PATH}")
        return 1

    print("--- TOOL CALL TRAIL (from audit JSONL) ---")
    tool_events: list[dict[str, Any]] = []
    for line in _AUDIT_PATH.read_text().splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("event") in {
            "tool_call_attempt",
            "tool_call_result",
        }:
            tool_events.append(evt)
    for evt in tool_events:
        tag = (
            "ATTEMPT" if evt["event"] == "tool_call_attempt" else "RESULT "
        )
        name = evt.get("tool_name")
        if evt["event"] == "tool_call_attempt":
            args = evt.get("tool_args") or {}
            print(f"  {tag}  {name}({json.dumps(args, sort_keys=True)})")
        else:
            print(f"  {tag}  {name} → status={evt.get('result_status')}")
    print()

    print("--- AUDIT JSONL EVENT TYPES ---")
    by_event: dict[str, int] = {}
    for line in _AUDIT_PATH.read_text().splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_event[evt.get("event", "?")] = by_event.get(evt.get("event", "?"), 0) + 1
    for name, count in sorted(by_event.items()):
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
