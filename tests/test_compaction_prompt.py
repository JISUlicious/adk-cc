"""Phase 1 tests: compaction prompt resolution + analysis-strip. Model-free."""

from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.agent import (
    _ADKCC_COMPACTION_PROMPT,
    _COMPACTION_PLACEHOLDER,
    _resolve_compaction_prompt,
    _strip_analysis,
)


def _event(text: str):
    """Synthetic compaction Event shaped like ADK's (actions.compaction.
    compacted_content is a Content with parts[].text)."""
    part = SimpleNamespace(text=text)
    content = SimpleNamespace(parts=[part], role="model")
    compaction = SimpleNamespace(compacted_content=content)
    return SimpleNamespace(actions=SimpleNamespace(compaction=compaction))


def _text(ev) -> str:
    return ev.actions.compaction.compacted_content.parts[0].text


# ---------- prompt resolution ----------
def test_default_prompt_has_single_placeholder_and_no_stray_braces():
    p = _ADKCC_COMPACTION_PROMPT
    assert p.count(_COMPACTION_PLACEHOLDER) == 1, "need exactly one placeholder"
    # remove the one valid placeholder; there must be NO other braces left
    rest = p.replace(_COMPACTION_PLACEHOLDER, "")
    assert "{" not in rest and "}" not in rest, "stray braces break str.format"


def test_default_template_formats_cleanly():
    # the real interpolation ADK does
    out = _resolve_compaction_prompt().format(conversation_history="HELLO-HISTORY")
    assert "HELLO-HISTORY" in out


def test_env_inline_override():
    os.environ["ADK_CC_COMPACTION_PROMPT"] = "Custom: {conversation_history}"
    try:
        assert _resolve_compaction_prompt() == "Custom: {conversation_history}"
    finally:
        os.environ.pop("ADK_CC_COMPACTION_PROMPT", None)


def test_env_file_override():
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"FromFile: {conversation_history}")
    os.close(fd)
    os.environ["ADK_CC_COMPACTION_PROMPT_FILE"] = path
    try:
        assert "FromFile:" in _resolve_compaction_prompt()
    finally:
        os.environ.pop("ADK_CC_COMPACTION_PROMPT_FILE", None)
        os.remove(path)


def test_missing_placeholder_is_appended():
    os.environ["ADK_CC_COMPACTION_PROMPT"] = "No placeholder here"
    try:
        t = _resolve_compaction_prompt()
        assert _COMPACTION_PLACEHOLDER in t
        t.format(conversation_history="X")  # must not raise
    finally:
        os.environ.pop("ADK_CC_COMPACTION_PROMPT", None)


# ---------- analysis strip ----------
def test_strip_removes_analysis_and_unwraps_summary():
    ev = _event(
        "<analysis>\nchain of thought, file foo.py, error X\n</analysis>\n"
        "<summary>\n1. Primary Request: do the thing.\n</summary>"
    )
    _strip_analysis(ev)
    out = _text(ev)
    assert "chain of thought" not in out and "<analysis>" not in out
    assert "<summary>" not in out and "Primary Request" in out


def test_strip_no_summary_tag_keeps_text_minus_analysis():
    ev = _event("<analysis>scratch</analysis>\nJust a plain summary, no tags.")
    _strip_analysis(ev)
    out = _text(ev)
    assert "scratch" not in out and "plain summary" in out


def test_strip_no_tags_passthrough():
    ev = _event("A plain summary with no tags at all.")
    _strip_analysis(ev)
    assert _text(ev) == "A plain summary with no tags at all."


def test_strip_never_empties():
    # only-analysis output → must not become empty (degrade to raw)
    ev = _event("<analysis>only analysis, no summary block</analysis>")
    _strip_analysis(ev)
    assert _text(ev).strip() != ""


def test_strip_tolerates_missing_compaction():
    ev = SimpleNamespace(actions=SimpleNamespace(compaction=None))
    # must not raise
    _strip_analysis(ev)
    ev2 = SimpleNamespace(actions=None)
    _strip_analysis(ev2)


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall compaction-prompt tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
