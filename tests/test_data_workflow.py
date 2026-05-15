"""E2E test for `examples/data_workflow.py`.

Boots the demo as a subprocess and asserts:

  - exit code == 0
  - the 5 expected tool calls appear in the audit trail in order
  - each tool returns `status=ok`
  - the LLM threads results between calls (5 model_request/response
    pairs for the tool turns + 1 final text turn = 6 total)
  - the final text response is captured in the demo's stdout
  - the project_context_loaded audit event fires (plugin chain
    proven alive)

Run: `.venv/bin/python tests/test_data_workflow.py`
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEMO = _REPO_ROOT / "examples" / "data_workflow.py"

# The exact tool-call sequence the scripted LLM drives. If a future
# refactor changes any of these, the demo's prose and this list must
# update together.
_EXPECTED_SEQUENCE = [
    "load_employees",
    "filter_by_department",
    "filter_by_min_salary",
    "sort_by_salary",
    "summarize_salary",
]


def _run(script: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ADK_CC_API_KEY": "sk-dummy-for-tests",
            "ADK_CC_LOG_LEVEL": "INFO",
        },
        cwd=str(_REPO_ROOT),
        timeout=180,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _section(stdout: str, header: str) -> list[str]:
    """Return the lines under a `--- HEADER ---` section in the demo
    output, stopping at the next blank line."""
    lines: list[str] = []
    started = False
    target = f"--- {header} ---"
    for line in stdout.splitlines():
        if line.strip() == target:
            started = True
            continue
        if started:
            if not line.strip():
                break
            lines.append(line.strip())
    return lines


def _parse_event_counts(stdout: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in _section(stdout, "AUDIT JSONL EVENT TYPES"):
        m = re.match(r"^([a-z_]+):\s*(\d+)$", line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def _parse_tool_trail(stdout: str) -> list[tuple[str, str]]:
    """Return ordered (tag, tool_name) pairs from the TOOL CALL TRAIL."""
    pairs: list[tuple[str, str]] = []
    for line in _section(stdout, "TOOL CALL TRAIL (from audit JSONL)"):
        m = re.match(r"^(ATTEMPT|RESULT)\s+([a-z_]+)", line)
        if m:
            pairs.append((m.group(1), m.group(2)))
    return pairs


def test_workflow_runs_clean() -> None:
    rc, stdout, stderr = _run(_DEMO)
    assert rc == 0, (
        f"demo exited non-zero: rc={rc}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    # Final-text section present + non-empty.
    final = _section(stdout, "FINAL MODEL TEXT")
    assert final, f"missing FINAL MODEL TEXT section in:\n{stdout}"
    assert any("$106,666.67" in ln for ln in final), (
        f"expected scripted final reply with avg, got: {final}"
    )
    print("[runs_clean] passed")


def test_tool_sequence_matches_expected() -> None:
    _, stdout, _ = _run(_DEMO)
    pairs = _parse_tool_trail(stdout)
    # We expect ATTEMPT then RESULT for each tool, in the scripted order.
    attempts = [name for tag, name in pairs if tag == "ATTEMPT"]
    results = [name for tag, name in pairs if tag == "RESULT"]
    assert attempts == _EXPECTED_SEQUENCE, (
        f"attempt sequence mismatch:\n  expected: {_EXPECTED_SEQUENCE}\n"
        f"  got:      {attempts}"
    )
    assert results == _EXPECTED_SEQUENCE, (
        f"result sequence mismatch:\n  expected: {_EXPECTED_SEQUENCE}\n"
        f"  got:      {results}"
    )
    # Interleaved order: each ATTEMPT immediately followed by its RESULT.
    flat = [(t, n) for t, n in pairs]
    for i in range(0, len(flat), 2):
        assert flat[i][0] == "ATTEMPT" and flat[i + 1][0] == "RESULT", (
            f"non-interleaved pair at index {i}: {flat[i:i+2]}"
        )
        assert flat[i][1] == flat[i + 1][1], (
            f"attempt/result name mismatch at index {i}: "
            f"{flat[i]} vs {flat[i+1]}"
        )
    print("[tool_sequence] passed")


def test_audit_event_counts() -> None:
    _, stdout, _ = _run(_DEMO)
    counts = _parse_event_counts(stdout)
    # 5 tool-driving turns + 1 final-text turn = 6 model round trips.
    assert counts.get("model_request") == 6, (
        f"expected 6 model_request events, got {counts.get('model_request')!r}\n"
        f"counts={counts}"
    )
    assert counts.get("model_response") == 6, (
        f"expected 6 model_response events, got {counts.get('model_response')!r}\n"
        f"counts={counts}"
    )
    assert counts.get("tool_call_attempt") == 5, (
        f"expected 5 tool_call_attempt events, got {counts.get('tool_call_attempt')!r}\n"
        f"counts={counts}"
    )
    assert counts.get("tool_call_result") == 5, (
        f"expected 5 tool_call_result events, got {counts.get('tool_call_result')!r}\n"
        f"counts={counts}"
    )
    assert counts.get("project_context_loaded") == 1, (
        f"expected 1 project_context_loaded event, got {counts.get('project_context_loaded')!r}\n"
        f"counts={counts}"
    )
    # No errors crept in.
    assert counts.get("tool_call_error", 0) == 0, (
        f"unexpected tool_call_error in counts: {counts}"
    )
    print("[event_counts] passed")


def main() -> None:
    assert _DEMO.exists(), f"missing demo at {_DEMO}"
    test_workflow_runs_clean()
    test_tool_sequence_matches_expected()
    test_audit_event_counts()
    print("\nall data-workflow tests passed")


if __name__ == "__main__":
    main()
