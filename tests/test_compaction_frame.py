"""Phase 5 tests: continuation framing on the compaction summary. Model-free."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.agent import _COMPACTION_FRAME_DEFAULT, _frame_summary


def _event(text="1. Primary Request: build a service."):
    part = SimpleNamespace(text=text)
    content = SimpleNamespace(parts=[part], role="model")
    return SimpleNamespace(actions=SimpleNamespace(
        compaction=SimpleNamespace(compacted_content=content)))


def _text(ev):
    return ev.actions.compaction.compacted_content.parts[0].text


def _save(*keys):
    return {k: os.environ.get(k) for k in keys}


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_frame_prepended_by_default():
    saved = _save("ADK_CC_COMPACTION_FRAME")
    os.environ.pop("ADK_CC_COMPACTION_FRAME", None)
    try:
        ev = _event()
        _frame_summary(ev)
        out = _text(ev)
        assert out.startswith(_COMPACTION_FRAME_DEFAULT), out[:40]
        assert "Primary Request" in out  # original summary still present, after
    finally:
        _restore(saved)


def test_frame_disabled_with_zero():
    saved = _save("ADK_CC_COMPACTION_FRAME")
    os.environ["ADK_CC_COMPACTION_FRAME"] = "0"
    try:
        ev = _event()
        before = _text(ev)
        _frame_summary(ev)
        assert _text(ev) == before, "FRAME=0 → no prepend"
    finally:
        _restore(saved)


def test_frame_custom_override():
    saved = _save("ADK_CC_COMPACTION_FRAME")
    os.environ["ADK_CC_COMPACTION_FRAME"] = "CONTINUE-NOW."
    try:
        ev = _event()
        _frame_summary(ev)
        assert _text(ev).startswith("CONTINUE-NOW.")
    finally:
        _restore(saved)


def test_frame_tolerates_missing_compaction():
    _frame_summary(SimpleNamespace(actions=SimpleNamespace(compaction=None)))
    _frame_summary(SimpleNamespace(actions=None))  # must not raise


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
    print("\nall compaction-frame tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
