"""E2E test for `examples/data_science_agent.py`.

Subprocess-boots the demo and asserts:

  - exit code == 0
  - the six specialist transfers fire in the expected order
    (loader, loader, explorer, processor, processor, visualizer)
  - the five loop-stage transitions land in the audit JSONL
    (∅→explore→reason→act→verify→done)
  - audit event counts match: 17 tool attempts + 17 results, no errors
  - the coordinator's final text includes the conclusion from the
    verify step (proves the run reached and passed VERIFY)

Run: `.venv/bin/python tests/test_data_science_agent.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO = _REPO_ROOT / "examples" / "data_science_agent.py"

_EXPECTED_TRANSFERS = [
    "loader",
    "loader",
    "explorer",
    "processor",
    "processor",
    "visualizer",
]

# (from_stage, to_stage) — `None` from the JSON serializes to the
# string "None" in the demo's "from → to" print, hence we match the
# rendered prefix below.
_EXPECTED_TRANSITIONS = [
    ("∅", "explore"),       # ∅ → explore (None render)
    ("explore", "reason"),
    ("reason", "act"),
    ("act", "verify"),
    ("verify", "done"),
]


def _run() -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(_DEMO)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ADK_CC_API_KEY": "sk-dummy-for-tests",
            "ADK_CC_LOG_LEVEL": "INFO",
        },
        cwd=str(_REPO_ROOT),
        timeout=300,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _section(stdout: str, header: str) -> list[str]:
    """Return lines under a `--- HEADER ---` block."""
    out: list[str] = []
    started = False
    target = f"--- {header} ---"
    for line in stdout.splitlines():
        if line.strip() == target:
            started = True
            continue
        if started:
            if not line.strip():
                break
            out.append(line.strip())
    return out


def _parse_transfers(stdout: str) -> list[str]:
    return [
        m.group(1)
        for m in (
            re.match(r"^→\s+(\S+)$", ln) for ln in _section(stdout, "TRANSFER SEQUENCE")
        )
        if m is not None
    ]


def _parse_transitions(stdout: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for ln in _section(stdout, "LOOP STAGE TRANSITIONS"):
        m = re.match(r"^(\S+)\s+→\s+(\S+)$", ln)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _parse_event_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ln in _section(stdout, "AUDIT EVENT COUNTS"):
        m = re.match(r"^([a-z_]+):\s*(\d+)$", ln)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def test_demo_exits_clean() -> None:
    rc, stdout, stderr = _run()
    assert rc == 0, (
        f"demo exited non-zero: rc={rc}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    print("[exits_clean] passed")


def test_transfer_sequence_matches() -> None:
    _, stdout, _ = _run()
    transfers = _parse_transfers(stdout)
    assert transfers == _EXPECTED_TRANSFERS, (
        f"transfer sequence mismatch:\n"
        f"  expected: {_EXPECTED_TRANSFERS}\n"
        f"  got:      {transfers}"
    )
    print("[transfer_sequence] passed")


def test_loop_traverses_all_stages() -> None:
    _, stdout, _ = _run()
    transitions = _parse_transitions(stdout)
    assert transitions == _EXPECTED_TRANSITIONS, (
        f"stage transitions mismatch:\n"
        f"  expected: {_EXPECTED_TRANSITIONS}\n"
        f"  got:      {transitions}"
    )
    # The final state must be `done` — verify_completion returned PASS.
    assert transitions[-1][1] == "done", (
        f"loop did not finish — final transition: {transitions[-1]}"
    )
    print("[traverses_all_stages] passed")


def test_audit_event_counts() -> None:
    _, stdout, _ = _run()
    counts = _parse_event_counts(stdout)
    assert counts.get("loop_stage_transition") == 5, (
        f"expected 5 loop_stage_transition events, got "
        f"{counts.get('loop_stage_transition')!r}"
    )
    # The exact tool-call count depends on ADK's `_handback_to_coordinator`
    # synthetic events too. Lower-bound: 11 actual tool calls
    # (3 loader+explorer + 2 plan steps + 3 acting + 3 marks + 1 verify
    # + 1 record_plan — sums vary by how ADK counts transfer-tool calls).
    # Strict equality on the observed-from-demo value (17) keeps us
    # honest about regressions.
    assert counts.get("tool_call_attempt") == 17, (
        f"expected 17 tool_call_attempt events, got "
        f"{counts.get('tool_call_attempt')!r}\n  counts={counts}"
    )
    assert counts.get("tool_call_result") == 17, (
        f"expected 17 tool_call_result events, got "
        f"{counts.get('tool_call_result')!r}\n  counts={counts}"
    )
    assert counts.get("tool_call_error", 0) == 0, (
        f"unexpected tool_call_error events: {counts}"
    )
    assert counts.get("loop_stage_block", 0) == 0, (
        f"unexpected loop_stage_block events (stage guard rejected "
        f"something): {counts}"
    )
    print("[audit_counts] passed")


def test_final_text_contains_conclusion() -> None:
    _, stdout, _ = _run()
    final = _section(stdout, "FINAL COORDINATOR TEXT")
    joined = " ".join(final)
    # The scripted conclusion mentions "$530,000" and "$545,000" — both
    # numbers come from the processor's aggregate results that the agent
    # threaded through its plan steps.
    assert "$530,000" in joined and "$545,000" in joined, (
        f"final text missing the expected revenue figures:\n  {final}"
    )
    print("[final_text] passed")


def main() -> None:
    assert _DEMO.exists(), f"missing demo at {_DEMO}"
    test_demo_exits_clean()
    test_transfer_sequence_matches()
    test_loop_traverses_all_stages()
    test_audit_event_counts()
    test_final_text_contains_conclusion()
    print("\nall data-science agent tests passed")


if __name__ == "__main__":
    main()
