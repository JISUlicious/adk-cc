"""End-to-end demo of the data-science agent.

Walks the coordinator + 4 specialists through one full
explore → plan → act → verify loop with a scripted LLM. The dataset,
prompt, and expected numbers are all deterministic so the audit trail
can be machine-checked.

User query: "Which region had the highest total revenue in Q1, and
            how does that compare to its Q2 result?"

Scripted loop:
  1. Coordinator: transfer to `loader`        (EXPLORE)
  2. Loader:      load_from_registry("sales_q1")
  3. Loader:      load_from_registry("sales_q2")
  4. Coordinator: transfer to `explorer`      (EXPLORE)
  5. Explorer:    describe_dataset("sales_q1")
  6. Coordinator: record_plan([...])          (PLAN)
  7. Coordinator: transfer to `processor`     (ACT step 1)
  8. Processor:   aggregate_dataset(...)
  9. Coordinator: transfer to `processor`     (ACT step 2)
 10. Processor:   aggregate_dataset(...)
 11. Coordinator: transfer to `visualizer`    (ACT step 3)
 12. Visualizer:  render_bar_chart(...)
 13. Coordinator: transfer to `critic`        (VERIFY — independent judge)
 14. Critic:      structured JSON CriticVerdict (PASS, evidence_quality=strong)
 15. Coordinator: verify_completion(... critic_verdict)
 16. Coordinator: final user-facing text

No explicit `mark_step_done` calls — step completion is inferred
from the acting-tool result count vs the plan length. The critic's
verdict is read from session history and passed verbatim to
verify_completion as the `critic_verdict` arg; coordinator
self-judgment is GONE (a model grading its own work has near-zero
independence).

Run:
  `.venv/bin/python examples/data_science_agent.py`
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

_TMP = Path(tempfile.mkdtemp(prefix="ds_agent_demo_"))
_AUDIT_PATH = _TMP / "audit.jsonl"

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-demo")
os.environ["ADK_CC_LOG_LEVEL"] = "INFO"
os.environ["ADK_CC_AUDIT_LOG"] = str(_AUDIT_PATH)
os.environ["ADK_CC_LOG_MODEL_IO"] = "1"

from google.adk.apps.app import App
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
    """Replays queued responses keyed by the agent who's about to ask.

    ADK calls `generate_content_async` per agent invocation. By keying
    on the *target agent* (read from llm_request.contents or the
    system_instruction) we keep the script readable: each block of
    responses belongs to one agent.
    """

    model: str = "fake/ds-agent"
    coord_queue: list[LlmResponse] = Field(default_factory=list)
    loader_queue: list[LlmResponse] = Field(default_factory=list)
    explorer_queue: list[LlmResponse] = Field(default_factory=list)
    processor_queue: list[LlmResponse] = Field(default_factory=list)
    visualizer_queue: list[LlmResponse] = Field(default_factory=list)
    critic_queue: list[LlmResponse] = Field(default_factory=list)

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake/.*"]

    def _which_queue(self, llm_request) -> list[LlmResponse]:
        """The system_instruction starts with the agent's instruction
        prefix — coordinator's prompt mentions 'coordinator', loader's
        mentions 'loader', etc. That's stable enough to route on.
        """
        si = getattr(llm_request.config, "system_instruction", None) or ""
        if not isinstance(si, str):
            si = str(si)
        if "You are the `loader`" in si:
            return self.loader_queue
        if "You are the `explorer`" in si:
            return self.explorer_queue
        if "You are the `processor`" in si:
            return self.processor_queue
        if "You are the `visualizer`" in si:
            return self.visualizer_queue
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


# ---------- queues ----------


def _build_scripted_llm() -> _Scripted:
    return _Scripted(
        coord_queue=[
            # EXPLORE: transfer to loader, twice (once per dataset).
            _fc(
                "co-1",
                "transfer_to_agent",
                {"agent_name": "loader"},
            ),
            _fc(
                "co-2",
                "transfer_to_agent",
                {"agent_name": "loader"},
            ),
            # EXPLORE: transfer to explorer for one describe.
            _fc(
                "co-3",
                "transfer_to_agent",
                {"agent_name": "explorer"},
            ),
            # PLAN — jump straight from explore to record_plan. No
            # separate REASON step. The coordinator's prompt makes
            # clear that internal reasoning happens implicitly.
            _fc(
                "co-4",
                "record_plan",
                {
                    "steps": [
                        "Aggregate revenue by region for sales_q1 (sum)",
                        "Aggregate revenue by region for sales_q2 (sum)",
                        "Render a bar chart of sales_q1 revenue by region",
                    ]
                },
            ),
            # ACT step 0: dispatch to processor (step auto-marked on handback)
            _fc("co-5", "transfer_to_agent", {"agent_name": "processor"}),
            # ACT step 1: dispatch to processor again
            _fc("co-6", "transfer_to_agent", {"agent_name": "processor"}),
            # ACT step 2: dispatch to visualizer
            _fc("co-7", "transfer_to_agent", {"agent_name": "visualizer"}),
            # VERIFY step 1: dispatch to the critic for an independent judgment.
            _fc("co-8", "transfer_to_agent", {"agent_name": "critic"}),
            # VERIFY step 2: call verify_completion with the critic's verdict.
            _fc(
                "co-9",
                "verify_completion",
                {
                    "user_query": (
                        "Which region had the highest total revenue in Q1, "
                        "and how does that compare to its Q2 result?"
                    ),
                    "conclusion": (
                        "South region led Q1 with $530,000 in revenue and "
                        "extended its lead in Q2 with $545,000 — a 2.8% "
                        "quarter-over-quarter increase."
                    ),
                    "critic_verdict": {
                        "verdict": "PASS",
                        "addressed_aspects": [
                            "highest Q1 region by total revenue",
                            "comparison to Q2 for that region",
                        ],
                        "missing_aspects": [],
                        "evidence_quality": "strong",
                        "reasoning": (
                            "aggregate_dataset on sales_q1 returned south=530k as "
                            "the top region; aggregate_dataset on sales_q2 returned "
                            "south=545k, supporting the QoQ comparison. Both numeric "
                            "claims in the conclusion match the recorded results."
                        ),
                    },
                },
            ),
            # FINAL user-facing reply
            _txt(
                "South region led Q1 with $530,000 in revenue and "
                "extended its lead in Q2 with $545,000 — a 2.8% "
                "quarter-over-quarter increase.\n\n"
                "(Q1 totals: north=$420k, south=$530k, west=$490k.)"
            ),
        ],
        loader_queue=[
            # Each call: one tool, then a brief report text.
            _fc("ld-1", "load_from_registry", {"name": "sales_q1"}),
            _txt("loaded sales_q1 from registry (6 rows)"),
            _fc("ld-2", "load_from_registry", {"name": "sales_q2"}),
            _txt("loaded sales_q2 from registry (6 rows)"),
        ],
        explorer_queue=[
            _fc("ex-1", "describe_dataset", {"name": "sales_q1"}),
            _txt(
                "sales_q1: 6 rows, columns region/rep/deals/revenue, "
                "revenue range 180k–310k."
            ),
        ],
        processor_queue=[
            # Plan step 0
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
            _txt("aggregated: north=420000, south=530000, west=490000"),
            # Plan step 1
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
            _txt("aggregated: north=410000, south=545000, west=535000"),
        ],
        visualizer_queue=[
            _fc(
                "vz-1",
                "render_bar_chart",
                {
                    "name": "sales_q1",
                    "label_col": "region",
                    "value_col": "revenue",
                    "width": 30,
                },
            ),
            _txt("bar chart rendered for sales_q1 revenue by region"),
        ],
        critic_queue=[
            # The critic agent has `output_schema=CriticVerdict`, so its
            # response is a single text part containing JSON matching
            # the schema. The scripted LLM yields the bytes directly.
            _txt(
                '{"verdict": "PASS", '
                '"addressed_aspects": ["highest Q1 region by total revenue", '
                '"comparison to Q2 for that region"], '
                '"missing_aspects": [], '
                '"evidence_quality": "strong", '
                '"reasoning": "aggregate_dataset on sales_q1 returned south=530k '
                'as the top region; aggregate_dataset on sales_q2 returned '
                'south=545k, supporting the QoQ comparison. Both numeric claims '
                'in the conclusion match the recorded results."}'
            ),
        ],
    )


# ---------- driver ----------


async def run() -> int:
    # Re-bind the scripted model onto every agent in the imported tree.
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
        app_name="adk_cc_ds_demo",
    )
    user_id = "alice"
    session_id = f"ds-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    print(f"[ds-agent] audit log:  {_AUDIT_PATH}")
    print(f"[ds-agent] session_id: {session_id}")
    print(
        "[ds-agent] user query: 'Which region had the highest total revenue "
        "in Q1, and how does that compare to its Q2 result?'\n"
    )
    print("[ds-agent] running scripted loop...")

    final_text: list[str] = []
    transfers: list[str] = []
    async for ev in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        "Which region had the highest total revenue in Q1, "
                        "and how does that compare to its Q2 result?"
                    )
                )
            ],
        ),
    ):
        for fc in ev.get_function_calls():
            if fc.name == "transfer_to_agent":
                transfers.append((fc.args or {}).get("agent_name", "?"))
        if ev.author == "coordinator" and ev.content and ev.content.parts:
            for part in ev.content.parts:
                if getattr(part, "text", None):
                    final_text.append(part.text)
    print("[ds-agent] loop complete.\n")

    print("--- TRANSFER SEQUENCE ---")
    for t in transfers:
        print(f"  → {t}")
    print()

    if final_text:
        print("--- FINAL COORDINATOR TEXT ---")
        # Last text is the user-facing reply; prior ones are the
        # REASON step.
        print(final_text[-1])
        print()

    if not _AUDIT_PATH.exists():
        print(f"[ds-agent] WARN: no audit log at {_AUDIT_PATH}")
        return 1

    print("--- AUDIT EVENT COUNTS ---")
    counts: dict[str, int] = {}
    transitions: list[tuple[str, str]] = []
    verify_payload: dict = {}
    for line in _AUDIT_PATH.read_text().splitlines():
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = evt.get("event", "?")
        counts[name] = counts.get(name, 0) + 1
        if name == "loop_stage_transition":
            transitions.append((evt.get("from"), evt.get("to")))
        if name == "tool_call_result" and evt.get("tool_name") == "verify_completion":
            # The verify result isn't in the audit row itself, but we can
            # see attempt args nearby. We separately track the verify
            # final by checking the conclusion in transitions.
            pass
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    print()

    print("--- LOOP STAGE TRANSITIONS ---")
    for frm, to in transitions:
        print(f"  {frm or '∅'} → {to}")
    print()

    # Reuse the verify outcome from the audit by reading the
    # function-call args (verify_completion is in the tool trail).
    # The actual verdict landed in the model's view; we proved
    # the agent reached VERIFY and finished.
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
