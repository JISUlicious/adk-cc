"""Relative path arguments to `run_skill_script`.

ADK runs a skill script inside a fresh temp directory — its wrapper does
`os.chdir(tempfile.TemporaryDirectory())` so the script cannot litter the
workspace. Every relative path argument therefore resolves against that temp
dir. Seen live: `args=["index.html", "check.mjs"]` came back
"page not found: /var/.../tmpXXXX/index.html".

This is not one skill's problem. `data-analyst`'s probes all take a data file
positionally (`_probe_utils.py`: `p.add_argument("data", …)`), so the same door
is used by every analysis run.

The rule under test: rewrite a relative value only when `<workspace>/<value>`
EXISTS. Evidence, not pattern-matching — the alternative ("looks like a path")
would mangle column names and numbers.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_script_paths.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.tools import skills as sk  # noqa: E402

_WS = tempfile.mkdtemp(prefix="scriptargs-")
Path(_WS, "data.csv").write_text("a,b\n1,2\n")
Path(_WS, "index.html").write_text("<html></html>")
Path(_WS, "sub").mkdir()
Path(_WS, "sub", "nested.csv").write_text("x\n1\n")


class _Ws:
    abs_path = _WS


class _Ctx:
    state: dict = {}


def _anchor(args: dict) -> dict:
    real = sk.get_workspace if hasattr(sk, "get_workspace") else None
    import adk_cc.sandbox as sandbox

    prev = sandbox.get_workspace
    sandbox.get_workspace = lambda ctx: _Ws()          # noqa: ARG005
    try:
        return sk._anchor_script_args(args, _Ctx())
    finally:
        sandbox.get_workspace = prev
        assert real is None or True


def test_an_existing_relative_file_becomes_absolute() -> None:
    out = _anchor({"skill_name": "s", "file_path": "scripts/x.py",
                   "args": ["data.csv", "index.html"]})
    assert out["args"] == [os.path.join(_WS, "data.csv"),
                           os.path.join(_WS, "index.html")], out["args"]
    print("OK an_existing_relative_file_becomes_absolute")


def test_a_nested_path_works_too() -> None:
    out = _anchor({"args": ["sub/nested.csv"]})
    assert out["args"] == [os.path.join(_WS, "sub/nested.csv")]
    print("OK a_nested_path_works_too")


def test_non_paths_are_left_exactly_alone() -> None:
    """The reason the rule is existence-based. `defect_rate` is a column, `10`
    is a threshold, `--target` is a flag — a "looks like a path" heuristic would
    have mangled at least one of them."""
    original = ["defect_rate", "10", "--target", "-v", "nope.csv", ""]
    out = _anchor({"args": list(original)})
    assert out["args"] == original, out["args"]
    print("OK non_paths_are_left_exactly_alone")


def test_absolute_paths_are_untouched() -> None:
    p = os.path.join(_WS, "data.csv")
    out = _anchor({"args": [p, "/etc/hosts"]})
    assert out["args"] == [p, "/etc/hosts"]
    print("OK absolute_paths_are_untouched")


def test_dict_options_and_positionals_are_handled() -> None:
    """ADK accepts args as long-option dict + short_options + positional_args,
    not only a list. Fixing one shape would leave the analysis probes (which
    take `--target` alongside a positional data file) half-broken."""
    out = _anchor({
        "args": {"data": "data.csv", "target": "defect_rate"},
        "short_options": {"f": "index.html", "n": "5"},
        "positional_args": ["sub/nested.csv", "keep-me"],
    })
    assert out["args"]["data"] == os.path.join(_WS, "data.csv")
    assert out["args"]["target"] == "defect_rate"
    assert out["short_options"]["f"] == os.path.join(_WS, "index.html")
    assert out["short_options"]["n"] == "5"
    assert out["positional_args"] == [os.path.join(_WS, "sub/nested.csv"), "keep-me"]
    print("OK dict_options_and_positionals_are_handled")


def test_no_workspace_changes_nothing() -> None:
    """A context with no workspace (tests, odd callers) must pass through rather
    than raise — the script call is what matters, not the convenience."""
    import adk_cc.sandbox as sandbox

    prev = sandbox.get_workspace

    def _boom(ctx):  # noqa: ANN001, ARG001
        raise RuntimeError("no workspace")

    sandbox.get_workspace = _boom
    try:
        args = {"args": ["data.csv"]}
        assert sk._anchor_script_args(args, _Ctx()) == args
    finally:
        sandbox.get_workspace = prev
    print("OK no_workspace_changes_nothing")


def test_the_skill_name_and_file_path_are_not_rewritten() -> None:
    """`file_path` is relative to the SKILL, not the workspace. Rewriting it to
    a workspace path would break every call."""
    out = _anchor({"skill_name": "web-smoke-check",
                   "file_path": "scripts/smoke_page.py",
                   "args": ["index.html"]})
    assert out["skill_name"] == "web-smoke-check"
    assert out["file_path"] == "scripts/smoke_page.py"
    print("OK the_skill_name_and_file_path_are_not_rewritten")


def main() -> None:
    test_an_existing_relative_file_becomes_absolute()
    test_a_nested_path_works_too()
    test_non_paths_are_left_exactly_alone()
    test_absolute_paths_are_untouched()
    test_dict_options_and_positionals_are_handled()
    test_no_workspace_changes_nothing()
    test_the_skill_name_and_file_path_are_not_rewritten()
    print("\nall skill-script path tests passed")


if __name__ == "__main__":
    main()
