"""Pins for user-facing style rules in the coordinator prompt.

Each rule here exists because a rendering or wording failure was seen live;
the pin keeps a prompt edit from silently dropping it.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_prompt_rules.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def main() -> int:
    from adk_cc.prompts import COORDINATOR_INSTRUCTION as P

    # Reported live: the agent wrote ranges/approximations with `~` ("~50%",
    # "3~5일"); remark-gfm renders a tilde pair as strikethrough, so the text
    # between two of them in a paragraph vanished into a strike.
    style = P.split("# Style", 1)[1] if "# Style" in P else ""
    check("Style section forbids `~` for about/ranges",
          "Never use `~`" in style and "strikethrough" in style)
    check("and names the replacement (en dash / the word)",
          "en dash" in style and "about 50%" in style)
    check("covers the CJK range tilde too", "3~5일" in style)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
