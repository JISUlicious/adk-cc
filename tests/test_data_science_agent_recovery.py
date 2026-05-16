"""E2E test for `examples/data_science_agent_recovery.py`.

Companion to `tests/test_data_science_agent.py` (which exercises
the happy path). This one verifies the critic-FAIL recovery loop:

  - The coordinator initially plans for only PART of the user's query
    (Q1 only when the user asked for Q1 AND Q2).
  - The critic catches the gap and returns FAIL with non-empty
    `missing_aspects`.
  - The coordinator re-plans + dispatches the missing computation.
  - A second critic invocation returns PASS.
  - `verify_completion` succeeds with the second critic's PASS verdict.

Subprocess-boots the demo and asserts:

  - exit code == 0
  - the six specialist transfers fire in the recovery order
    (loader, explorer, processor, critic, processor, critic)
  - the seven loop-stage transitions land, INCLUDING the backward
    `verify → act` jump that happens when record_plan is re-called
    after the critic-FAIL
  - audit event counts match: 13 tool attempts + 13 results
  - the critic's PASS verdict (the SECOND one) is what flows into
    verify_completion

Run: `.venv/bin/python tests/test_data_science_agent_recovery.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO = _REPO_ROOT / "examples" / "data_science_agent_recovery.py"

_EXPECTED_TRANSFERS = [
    "loader",
    "explorer",
    "processor",
    "critic",       # first critic invocation — returns FAIL
    "processor",    # recovery dispatch — runs the missing Q2 aggregate
    "critic",       # second critic invocation — returns PASS
]

# Backward `verify → act` is the signature of the recovery path:
# record_plan re-called from inside the verify stage drops the
# stage back to act so a second round of acting can run, then
# act → verify again, then verify_completion → done.
_EXPECTED_TRANSITIONS = [
    ("∅", "explore"),
    ("explore", "plan"),
    ("plan", "act"),
    ("act", "verify"),
    ("verify", "act"),       # recovery: re-plan after critic FAIL
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
            re.match(r"^→\s+(\S+)$", ln)
            for ln in _section(stdout, "TRANSFER SEQUENCE")
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


def _parse_critic_verdicts(stdout: str) -> list[str]:
    return [
        ln.strip()
        for ln in _section(stdout, "CRITIC VERDICT PASSED TO verify_completion")
    ]


def test_demo_exits_clean() -> None:
    rc, stdout, stderr = _run()
    assert rc == 0, (
        f"demo exited non-zero: rc={rc}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    print("[exits_clean] passed")


def test_recovery_transfer_sequence() -> None:
    _, stdout, _ = _run()
    transfers = _parse_transfers(stdout)
    assert transfers == _EXPECTED_TRANSFERS, (
        f"transfer sequence mismatch:\n"
        f"  expected: {_EXPECTED_TRANSFERS}\n"
        f"  got:      {transfers}"
    )
    # Critic was dispatched twice, confirming the recovery loop ran.
    assert transfers.count("critic") == 2, (
        f"expected 2 critic transfers (FAIL + PASS), got "
        f"{transfers.count('critic')}: {transfers}"
    )
    print("[recovery_transfer_sequence] passed")


def test_backward_verify_to_act_transition() -> None:
    _, stdout, _ = _run()
    transitions = _parse_transitions(stdout)
    assert transitions == _EXPECTED_TRANSITIONS, (
        f"stage transitions mismatch:\n"
        f"  expected: {_EXPECTED_TRANSITIONS}\n"
        f"  got:      {transitions}"
    )
    # The signature of the recovery path: a verify → act jump exists.
    backward = [
        (frm, to) for frm, to in transitions if (frm, to) == ("verify", "act")
    ]
    assert backward, (
        f"expected at least one verify → act backward transition (the "
        f"recovery signature), got transitions: {transitions}"
    )
    assert transitions[-1] == ("verify", "done"), (
        f"loop did not finish — final transition: {transitions[-1]}"
    )
    print("[backward_verify_to_act] passed")


def test_audit_event_counts() -> None:
    _, stdout, _ = _run()
    counts = _parse_event_counts(stdout)
    # 7 transitions (incl. the backward jump), 13 tool calls, no errors.
    assert counts.get("loop_stage_transition") == 7, (
        f"expected 7 loop_stage_transition events, got "
        f"{counts.get('loop_stage_transition')!r}\n  counts={counts}"
    )
    assert counts.get("tool_call_attempt") == 13, (
        f"expected 13 tool_call_attempt events, got "
        f"{counts.get('tool_call_attempt')!r}\n  counts={counts}"
    )
    assert counts.get("tool_call_result") == 13, (
        f"expected 13 tool_call_result events, got "
        f"{counts.get('tool_call_result')!r}\n  counts={counts}"
    )
    assert counts.get("tool_call_error", 0) == 0, (
        f"unexpected tool_call_error events: {counts}"
    )
    print("[audit_counts] passed")


def test_verify_received_pass_verdict() -> None:
    _, stdout, _ = _run()
    verdicts = _parse_critic_verdicts(stdout)
    # The coordinator only calls verify_completion ONCE (after the
    # second critic invocation returned PASS). If verify were called
    # with a FAIL verdict somewhere, this would catch it.
    assert verdicts == ["PASS"], (
        f"expected verify_completion to receive exactly one PASS "
        f"critic verdict, got: {verdicts}"
    )
    print("[verify_received_pass] passed")


def test_final_text_reflects_recovery() -> None:
    _, stdout, _ = _run()
    final = _section(stdout, "FINAL COORDINATOR TEXT")
    joined = " ".join(final)
    # The final answer should reference Q2 numbers — proof the recovery
    # actually delivered the second computation, not just papered over.
    for marker in ("$535,000", "+9.2%", "$545k"):
        assert marker in joined, (
            f"final text missing recovery-derived figure {marker!r}:\n"
            f"  {final}"
        )
    print("[final_text_recovery] passed")


def main() -> None:
    assert _DEMO.exists(), f"missing demo at {_DEMO}"
    test_demo_exits_clean()
    test_recovery_transfer_sequence()
    test_backward_verify_to_act_transition()
    test_audit_event_counts()
    test_verify_received_pass_verdict()
    test_final_text_reflects_recovery()
    print("\nall data-science agent recovery tests passed")


if __name__ == "__main__":
    main()
