"""Dependencies are collected up front, shown on the card, installed once.

The measured need: `pdf` (published) dies on `No module named 'pypdf'`;
docx/pptx/xlsx need defusedxml; only 2 of 17 published skills ship a
requirements.txt and 0 of 41 set `compatibility`. So the collector reads the
manifest when there is one and the scripts' own imports otherwise — and never,
by decision, a runtime ModuleNotFoundError (that path turns typos into
installs).

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_deps.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

_ROOT = Path(tempfile.mkdtemp(prefix="depskills-"))
os.environ["ADK_CC_SKILLS_DIR"] = str(_ROOT)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


def _skill(name: str, files: dict[str, str]) -> None:
    d = _ROOT / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\nBody.\n")
    for rel, body in files.items():
        (d / "scripts" / rel).write_text(body)


# The pdf-skill shape: imports only, no manifest.
_skill("pdf-like", {"extract.py": (
    "import sys\nimport json\nfrom pypdf import PdfReader\n"
    "import _helpers\nprint('x')\n"), "_helpers.py": "X = 1\n"})
# The mcp-builder shape: a shipped manifest, plus an import the manifest
# already covers and one it does not.
_skill("manifested", {
    "requirements.txt": "# pinned by the author\nanthropic>=0.39.0\nmcp>=1.1.0\n",
    "run.py": "import anthropic\nimport yaml\n"})
# Everything satisfied elsewhere: stdlib + tier packages + siblings.
_skill("satisfied", {"go.py": (
    "import os, json\nimport numpy as np\nimport pandas as pd\n"
    "from sklearn import tree\nimport helper\n"), "helper.py": "pass\n"})
# Client report (mlcc): the dep lives in a SIBLING module, and bare
# psycopg2 is a source build no sandbox can compile.
_skill("sibling-dep", {
    "recommend.py": "import json\nimport common\nprint('x')\n",
    "common.py": "import psycopg2\n"})
# Client report variant: the sibling module sits at the SKILL ROOT, which
# ADK's resource loader never exposes.
_skill("root-dep", {"run.py": "import json\nimport common\n"})
(_ROOT / "root-dep" / "common.py").write_text("import psycopg2\n")
# Explicit declaration in SKILL.md metadata.
(_ROOT / "declared" / "scripts").mkdir(parents=True, exist_ok=True)
(_ROOT / "declared" / "SKILL.md").write_text(
    "---\nname: declared\ndescription: Declared reqs.\nmetadata:\n"
    "  x-adk-cc/requirements: |\n    psycopg2-binary>=2.9, requests\n---\n\nBody.\n")
(_ROOT / "declared" / "scripts" / "go.py").write_text("import requests\n")


def main() -> int:
    from adk_cc.tools import skills as sk
    from adk_cc.tools.skill_deps import collect_requirements

    sk.clear_project_skill_cache()
    skills = {s.frontmatter.name: s for s, _ in sk.discover_skills_with_sources()}

    check("an import-only skill yields its distribution",
          collect_requirements(skills["pdf-like"]) == ["pypdf"],
          collect_requirements(skills["pdf-like"]))
    got = collect_requirements(skills["manifested"])
    check("a shipped manifest is honoured verbatim, specifiers kept",
          "anthropic>=0.39.0" in got and "mcp>=1.1.0" in got, got)
    check("an import the manifest does not cover is still mapped (yaml→pyyaml)",
          "pyyaml" in got, got)
    check("stdlib, tier packages and siblings need nothing",
          collect_requirements(skills["satisfied"]) == [],
          collect_requirements(skills["satisfied"]))

    # --- the launcher embeds the install, once per version ---------------
    from adk_cc.tools.skills import _WiderScriptCodeExecutor

    ex = _WiderScriptCodeExecutor.__new__(_WiderScriptCodeExecutor)
    ex._script_timeout = 300
    code = ex._wrapper(".adk-cc/skill-runtime/pdf-like/abc", "scripts/extract.py",
                       [], None, ["numpy"], ["pypdf"])
    check("the wrapper carries the uv install for the declared deps",
          "'pip', 'install'" in code and "shutil.which('uv')" in code
          and "_deps = ['pypdf']" in code, code[:200])
    check("guarded by the once-per-version marker",
          ".deps-ok" in code, "(no marker)")
    check("into the session env, never system python",
          "sys.executable" in code and "pip3" not in code)
    empty = ex._wrapper(".adk-cc/skill-runtime/satisfied/abc", "scripts/go.py",
                        [], None, [], [])
    check("no deps, no install machinery runs",
          "_deps = []" in empty, "(deps list not empty)")

    # --- the card says so BEFORE the click --------------------------------
    from adk_cc.permissions.modes import PermissionMode
    from adk_cc.permissions.settings import SettingsHierarchy
    from adk_cc.plugins.permissions import PermissionPlugin

    toolset = sk.make_skill_toolset()
    tool = next(t for t in toolset._tools if t.name == "run_skill_script")

    class _Ctx:
        agent_name = "coordinator"

        def __init__(self):
            self.state = {}
            self.function_call_id = "c1"
            self.tool_confirmation = None
            self.requested = []
            self.actions = type("A", (), {"skip_summarization": False})()

        def request_confirmation(self, *, hint=None, payload=None):
            self.requested.append(payload)

    plugin = PermissionPlugin(SettingsHierarchy(),
                              default_mode=PermissionMode.DEFAULT)
    ctx = _Ctx()
    asyncio.run(plugin.before_tool_callback(
        tool=tool, tool_args={"skill_name": "pdf-like",
                              "file_path": "scripts/extract.py"},
        tool_context=ctx))
    detail = (ctx.requested[0] or {}).get("detail", "") if ctx.requested else ""
    check("the confirmation card lists what will be installed",
          "install" in detail and "pypdf" in detail, detail[-160:])
    ctx = _Ctx()
    asyncio.run(plugin.before_tool_callback(
        tool=tool, tool_args={"skill_name": "satisfied",
                              "file_path": "scripts/go.py"},
        tool_context=ctx))
    detail = (ctx.requested[0] or {}).get("detail", "") if ctx.requested else ""
    check("and stays quiet when nothing needs installing",
          "install into the analysis" not in detail, detail[-120:])

    # --- client-reported shapes (2026-08-19) ------------------------------
    got = collect_requirements(skills["sibling-dep"])
    check("a sibling module's import is found AND mapped to the wheel dist",
          got == ["psycopg2-binary"], got)
    got = collect_requirements(skills["root-dep"],
                               skill_dir=str(_ROOT / "root-dep"))
    check("a ROOT-level module is scanned for ITS imports (skill_dir)",
          "psycopg2-binary" in got, got)
    check("...and is never mistaken for a PyPI dist itself",
          "common" not in got, got)
    got_no_dir = collect_requirements(skills["root-dep"])
    check("without skill_dir the old (blind) behavior is unchanged",
          got_no_dir == ["common"], got_no_dir)
    got = collect_requirements(skills["declared"])
    check("x-adk-cc/requirements declaration wins, specifiers kept",
          got[0] == "psycopg2-binary>=2.9" and "requests" in got, got)

    # --- the wrapper announces the per-skill data dir ----------------------
    code = ex._wrapper(".adk-cc/skill-runtime/sibling-dep/abc",
                       "scripts/recommend.py", [], None, [], [],
                       skill_name="sibling-dep")
    check("wrapper exports SKILL_DATA_DIR + namespaced twin",
          "os.environ.setdefault('SKILL_DATA_DIR', _sd)" in code
          and "ADK_CC_SKILL_DATA_DIR" in code
          and "'skill-data', 'sibling-dep'" in code, code[:160])

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
