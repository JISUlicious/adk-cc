"""End-to-end demo: drop skills in `.adk-cc/skills/` and
`.claude/skills/` under a temp project, run the discovery, show what
`make_skill_toolset()` actually wires.

Five scenarios:

  1. Project-only `.adk-cc/skills/` — discovered without any env var.
  2. Both `.adk-cc/skills/` and `.claude/skills/` in the same dir —
     pick-one rule: only `.adk-cc/skills/` is in the resolved list.
  3. `.claude/skills/` alone — fallback half of pick-one works.
  4. `ADK_CC_SKILLS_DIR` shadows project skills — same skill name in
     env dir + project dir → env wins; project's version logged as
     shadowed.
  5. Full agent boot — `make_skill_toolset()` invoked, the toolset
     actually carries the project skill as a tool.

Run: `.venv/bin/python scripts/project_skills_demo.py`
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    inner = Path(__file__).parent / "_project_skills_demo_inner.py"
    result = subprocess.run(
        [sys.executable, str(inner)],
        env={
            **os.environ,
            "ADK_CC_API_KEY": "sk-dummy-for-demo",
            "ADK_CC_LOG_LEVEL": "INFO",
        },
        capture_output=True,
        text=True,
    )

    print(result.stdout)
    print("\n--- STDERR (filtered to skills + warnings) ---")
    for line in result.stderr.splitlines():
        if "authlib" in line or "FeatureName" in line or "check_feature_enabled" in line:
            continue
        if "skills" in line or "WARNING" in line or "ERROR" in line or "Traceback" in line:
            print(line)

    print("\nexit code:", result.returncode)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
