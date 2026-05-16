"""Critic-FAIL → coordinator recovery → critic-PASS demo.

Companion to `examples/data_science_agent.py`. Where the main demo
exercises the happy path (critic returns PASS first try), this one
exercises the recovery loop: the coordinator initially plans for
only PART of the user's query, the critic catches the missing
aspect and returns FAIL with non-empty `missing_aspects`, the
coordinator re-plans + dispatches the missing computation, then
re-dispatches to the critic which returns PASS.

User query: "Compare total revenue by region across Q1 AND Q2 so I
            know which region grew the most."

Scripted loop:
   1. Coordinator: transfer to `loader`           (EXPLORE)
   2. Loader:      load_from_registry("sales_q1")
   3. Coordinator: transfer to `explorer`          (EXPLORE)
   4. Explorer:    describe_dataset("sales_q1")
   5. Coordinator: record_plan(["Aggregate Q1 revenue by region"])  (PLAN)  — INCOMPLETE
   6. Coordinator: transfer to `processor`         (ACT step 1)
   7. Processor:   aggregate_dataset(sales_q1)
   8. Coordinator: transfer to `critic`            (VERIFY)
   9. Critic:      FAIL — missing_aspects=["Q2 region totals", "QoQ growth comparison"]
  10. Coordinator: record_plan([2 steps including Q2])  (RE-PLAN — verify→act)
  11. Coordinator: transfer to `processor`         (ACT step 2 — recovery)
  12. Processor:   aggregate_dataset(sales_q2)
  13. Coordinator: transfer to `critic`            (RE-VERIFY)
  14. Critic:      PASS — both Q1 and Q2 addressed
  15. Coordinator: verify_completion(... critic_verdict=PASS)
  16. Coordinator: final user-facing text

Run:
  `.venv/bin/python examples/data_science_agent_recovery.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import AsyncGenerator

_TMP = Path(tempfile.mkdtemp(prefix="ds_recovery_demo_"))
_AUDIT_PATH = _TMP / "audit.jsonl"

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-demo")
os.environ["ADK_CC_LOG_LEVEL"] = "INFO"
os.environ["ADK_CC_AUDIT_LOG"] = str(_AUDIT_PATH)
os.environ["ADK_CC_LOG_MODEL_IO"] = "1"

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import Field

from adk_cc import agent as ds_agent
from adk_cc.logging_setup import configure_logging

configure_logging()


# ---------- scripted LLM ----------


class _Scripted(BaseLlm):
    model: str = "fake/ds-recovery"
    coord_queue: list[LlmResponse] = Field(default_factory=list)
    loader_queue: list[LlmResponse] = Field(default_factory=list)
    explorer_queue: list[LlmResponse] = Field(default_factory=list)
    processor_queue: list[LlmResponse] = Field(default_factory=list)
    critic_queue: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    def _which_queue(self, llm_request) -> list[LlmResponse]:
        si = getattr(llm_request.config, "system_instruction", None) or ""
        if not isinstance(si, str):
            si = str(si)
        if "You are the `loader`" in si:
            return self.loader_queue
        if "You are the `explorer`" in si:
            return self.explorer_queue
        if "You are the `processor`" in si:
            return self.processor_queue
        if "You are the `critic`" in si:
            return self.critic_queue
        return self.coord_queue

    async def generate_content_async(
        self, llm_request, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        q = self._which_queue(llm_request)
        if not q:
            raise RuntimeError(
                f"queue empty for agent inferred from system_instruction\n"
                f"si head: {str(getattr(llm_request.config, 'system_instruction', ''))[:120]}"
            )
        yield q.pop(0)


def _fc(call_id: str, name: str, args: dict) -> LlmResponse:
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


def _txt(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model", parts=[types.Part(text=text)]
        ),
        partial=False,
    )


_USER_QUERY = (
    "Compare total revenue by region across Q1 AND Q2 so I know "
    "which region grew the most."
)


def _build_scripted_llm() -> _Scripted:
    return _Scripted(
        coord_queue=[
            # EXPLORE
            _fc("co-1", "transfer_to_agent", {"agent_name": "loader"}),
            _fc("co-2", "transfer_to_agent", {"agent_name": "explorer"}),
            # PLAN — INCOMPLETE on purpose (only Q1).
            _fc(
                "co-3",
                "record_plan",
                {"steps": ["Aggregate Q1 revenue by region"]},
            ),
            # ACT step 1
            _fc("co-4", "transfer_to_agent", {"agent_name": "processor"}),
            # VERIFY — critic catches the gap
            _fc("co-5", "transfer_to_agent", {"agent_name": "critic"}),
            # RECOVERY: re-plan with both quarters now.
            _fc(
                "co-6",
                "record_plan",
                {
                    "steps": [
                        "Aggregate Q1 revenue by region",
                        "Aggregate Q2 revenue by region",
                    ]
                },
            ),
            # ACT step 2 (recovery)
            _fc("co-7", "transfer_to_agent", {"agent_name": "processor"}),
            # RE-VERIFY
            _fc("co-8", "transfer_to_agent", {"agent_name": "critic"}),
            # verify_completion now with PASS verdict
            _fc(
                "co-9",
                "verify_completion",
                {
                    "user_query": _USER_QUERY,
                    "conclusion": (
                        "Across Q1 → Q2 revenue by region: south "
                        "$530,000 → $545,000 (+2.8%), west $490,000 → "
                        "$535,000 (+9.2%), north $420,000 → $410,000 "
                        "(-2.4%). West grew the most."
                    ),
                    "critic_verdict": {
                        "verdict": "PASS",
                        "addressed_aspects": [
                            "Q1 revenue by region",
                            "Q2 revenue by region",
                            "QoQ growth comparison",
                        ],
                        "missing_aspects": [],
                        "evidence_quality": "strong",
                        "reasoning": (
                            "Both Q1 and Q2 aggregates are recorded. "
                            "The conclusion's growth percentages match "
                            "the recorded totals (south +2.8%, west "
                            "+9.2%, north -2.4%). West is correctly "
                            "named as the highest grower."
                        ),
                    },
                },
            ),
            # FINAL user-facing reply
            _txt(
                "West grew the most across Q1 → Q2: $490,000 → $535,000 "
                "(+9.2%). South stayed top in absolute revenue ($530k "
                "→ $545k, +2.8%); north was the only region to shrink "
                "($420k → $410k, -2.4%)."
            ),
        ],
        loader_queue=[
            _fc("ld-1", "load_from_registry", {"name": "sales_q1"}),
            _txt("loaded sales_q1 from registry (6 rows)"),
        ],
        explorer_queue=[
            _fc("ex-1", "describe_dataset", {"name": "sales_q1"}),
            _txt(
                "sales_q1: 6 rows, columns region/rep/deals/revenue, "
                "revenue range 180k–310k."
            ),
        ],
        processor_queue=[
            # ACT step 1: Q1 aggregate (first attempt)
            _fc(
                "pr-1",
                "aggregate_dataset",
                {
                    "name": "sales_q1",
                    "group_by": "region",
                    "metric": "revenue",
                    "op": "sum",
                },
            ),
            _txt("aggregated Q1: north=420000, south=530000, west=490000"),
            # ACT step 2: Q2 aggregate (recovery dispatch)
            _fc(
                "pr-2",
                "aggregate_dataset",
                {
                    "name": "sales_q2",
                    "group_by": "region",
                    "metric": "revenue",
                    "op": "sum",
                },
            ),
            _txt("aggregated Q2: north=410000, south=545000, west=535000"),
        ],
        critic_queue=[
            # FIRST critique: FAIL — only Q1 covered, Q2 + growth comparison missing.
            _txt(
                '{"verdict": "FAIL", '
                '"addressed_aspects": ["Q1 revenue by region"], '
                '"missing_aspects": ["Q2 revenue by region", '
                '"QoQ growth comparison"], '
                '"evidence_quality": "insufficient", '
                '"reasoning": "Only sales_q1 was aggregated. The user '
                'asked to COMPARE Q1 AND Q2, but no Q2 result is '
                'recorded. The conclusion cannot identify which region '
                'grew the most without both quarters."}'
            ),
            # SECOND critique: PASS — both quarters covered, growth comparison present.
            _txt(
                '{"verdict": "PASS", '
                '"addressed_aspects": ["Q1 revenue by region", '
                '"Q2 revenue by region", "QoQ growth comparison"], '
                '"missing_aspects": [], '
                '"evidence_quality": "strong", '
                '"reasoning": "Both Q1 and Q2 aggregates are now '
                'recorded. The conclusion correctly identifies west '
                '(+9.2%) as the largest grower, with south growing '
                '+2.8% and north shrinking -2.4%. Numbers match the '
                'recorded results."}'
            ),
        ],
    )


# ---------- driver ----------


async def run() -> int:
    scripted = _build_scripted_llm()
    for ag in [
        ds_agent.root_agent,
        ds_agent.loader_agent,
        ds_agent.explorer_agent,
        ds_agent.processor_agent,
        ds_agent.visualizer_agent,
        ds_agent.critic_agent,
    ]:
        ag.model = scripted

    runner = InMemoryRunner(
        agent=ds_agent.root_agent,
        plugins=ds_agent.app.plugins,
        app_name="adk_cc_ds_recovery",
    )
    user_id = "alice"
    session_id = f"recovery-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    print(f"[ds-recovery] audit log:  {_AUDIT_PATH}")
    print(f"[ds-recovery] session_id: {session_id}")
    print(f"[ds-recovery] user query: '{_USER_QUERY}'\n")
    print("[ds-recovery] running scripted recovery loop...")

    final_text: list[str] = []
    transfers: list[str] = []
    async for ev in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=_USER_QUERY)],
        ),
    ):
        for fc in ev.get_function_calls():
            if fc.name == "transfer_to_agent":
                transfers.append((fc.args or {}).get("agent_name", "?"))
        if ev.author == "coordinator" and ev.content and ev.content.parts:
            for part in ev.content.parts:
                if getattr(part, "text", None):
                    final_text.append(part.text)
    print("[ds-recovery] loop complete.\n")

    print("--- TRANSFER SEQUENCE ---")
    for t in transfers:
        print(f"  → {t}")
    print()

    if final_text:
        print("--- FINAL COORDINATOR TEXT ---")
        print(final_text[-1])
        print()

    if not _AUDIT_PATH.exists():
        print(f"[ds-recovery] WARN: no audit log at {_AUDIT_PATH}")
        return 1

    print("--- AUDIT EVENT COUNTS ---")
    counts: dict[str, int] = {}
    transitions: list[tuple[str, str]] = []
    critic_verdicts: list[str] = []
    for line in _AUDIT_PATH.read_text().splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = evt.get("event", "?")
        counts[name] = counts.get(name, 0) + 1
        if name == "loop_stage_transition":
            transitions.append((evt.get("from"), evt.get("to")))
        if name == "tool_call_attempt" and evt.get("tool_name") == "verify_completion":
            args = evt.get("tool_args") or {}
            cv = args.get("critic_verdict")
            if isinstance(cv, dict):
                critic_verdicts.append(cv.get("verdict", "?"))
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    print()

    print("--- LOOP STAGE TRANSITIONS ---")
    for frm, to in transitions:
        print(f"  {frm or '∅'} → {to}")
    print()

    print("--- CRITIC VERDICT PASSED TO verify_completion ---")
    for v in critic_verdicts:
        print(f"  {v}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
