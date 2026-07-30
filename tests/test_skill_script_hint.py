"""Running a skill's script the wrong way has to say the right way.

Measured live: asked to find the driver in a CSV, the agent loaded
`data-analyst`, read SEVEN of its reference docs including `scripts/README.md`,
then ran

    python scripts/premodel_audit.py data.csv --target defect_rate --exclude lot
    → exit 2, "python: can't open file '/private/var/.../scripts/premodel_audit.py'"

and, getting nothing back but a file-not-found, wrote its own pandas analysis.
The answer was right. None of the six vetted probe scripts ran, and nothing in
the transcript said so.

The skill's own README documents that invocation (`python scripts/premodel_audit.py
data.csv --target SalePrice`) — correct outside adk-cc, impossible inside it,
where skill files are served through the skill tools rather than the filesystem.

So the failure now carries the correction. Enriching the failure rather than
intercepting the command: a pre-flight block would have to guess whether a
`scripts/…` path is the project's or a skill's, and this way a legitimate
command keeps its real exit code and output.

Run: ADK_CC_SKIP_DOTENV=1 .venv/bin/python tests/test_skill_script_hint.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.tools.bash.tool import _skill_script_hint  # noqa: E402
from adk_cc.tools.skills import locate_skill_script  # noqa: E402

_REAL_FAILURE = (
    "python: can't open file "
    "'/private/var/folders/x/T/proj/scripts/premodel_audit.py': "
    "[Errno 2] No such file or directory"
)


def test_the_verbatim_live_failure_is_explained() -> None:
    hint = _skill_script_hint(
        'export PATH="$PWD/.adk-cc/analysis-env/bin:$PATH"; '
        "python scripts/premodel_audit.py data.csv --target defect_rate --exclude lot",
        _REAL_FAILURE,
    )
    assert hint, "the exact command from the live run got no hint"
    assert "data-analyst" in hint, hint
    assert "run_skill_script" in hint and 'file_path="scripts/premodel_audit.py"' in hint, hint
    # It must also head off the two follow-on mistakes.
    assert "ABSOLUTE" in hint, hint          # temp cwd for skill scripts
    assert "re-implement" in hint, hint      # what it actually did instead
    print("OK the_verbatim_live_failure_is_explained")


def test_a_bare_script_name_also_resolves() -> None:
    hint = _skill_script_hint("python premodel_audit.py data.csv", _REAL_FAILURE)
    assert hint and "data-analyst" in hint
    print("OK a_bare_script_name_also_resolves")


def test_the_node_runner_is_covered_too() -> None:
    """The same mistake in the other direction — the skill that motivated all
    this shipped a .mjs and the agent tried `node scripts/smoke_page.mjs`."""
    hint = _skill_script_hint(
        "node scripts/smoke_page.mjs index.html check.mjs",
        "Error: Cannot find module '/tmp/proj/scripts/smoke_page.mjs'",
    )
    assert hint and "web-smoke-check" in hint, hint
    print("OK the_node_runner_is_covered_too")


def test_every_launchable_extension_is_redirectable() -> None:
    """The redirect and the launcher must agree on what a script IS.

    They didn't: the launcher grew `.ps1`/`.rb`/`.ts`, the hint's own regex kept
    listing py|sh|bash|mjs|js, and a skill shipping a PowerShell script would
    have failed in bash with no pointer to `run_skill_script` at all.
    """
    from adk_cc.tools.bash.tool import _scriptish_re
    from adk_cc.tools.skills import launchable_script_exts

    rx = _scriptish_re()
    for ext in launchable_script_exts():
        token = f"scripts/thing.{ext}"
        assert rx.findall(f"python {token} --flag") == [token], (
            f".{ext} is launchable but the hint cannot even see it as a script")
    print("OK every_launchable_extension_is_redirectable")


def test_an_ordinary_failure_is_left_alone() -> None:
    """No hint where there is nothing to redirect to — otherwise every failed
    command grows a paragraph nobody needs."""
    assert _skill_script_hint("ls nope", "ls: nope: No such file or directory") is None
    assert _skill_script_hint("python tools/mine.py", _REAL_FAILURE) is None
    assert _skill_script_hint("pytest -q", "2 failed, 1 passed") is None
    print("OK an_ordinary_failure_is_left_alone")


def test_a_succeeding_command_gets_nothing() -> None:
    """Keyed on the failure text: a project that legitimately HAS
    `scripts/premodel_audit.py` and runs it fine must not be second-guessed."""
    assert _skill_script_hint("python scripts/premodel_audit.py data.csv", "") is None
    assert _skill_script_hint("python scripts/premodel_audit.py data.csv",
                              "audit complete") is None
    print("OK a_succeeding_command_gets_nothing")


def test_an_absolute_path_is_not_hijacked() -> None:
    """An absolute path is a deliberate filesystem reference, not a
    skill-relative call — including one pointing INTO the installed skills."""
    assert _skill_script_hint(
        "python /opt/thing/scripts/premodel_audit.py", _REAL_FAILURE) is None
    print("OK an_absolute_path_is_not_hijacked")


def test_the_locator_matches_by_tail_not_by_guess() -> None:
    assert locate_skill_script("scripts/premodel_audit.py")[0] == "data-analyst"
    assert locate_skill_script("premodel_audit.py")[0] == "data-analyst"
    assert locate_skill_script("scripts/smoke_page.py")[0] == "web-smoke-check"
    assert locate_skill_script("scripts/not-a-real-script.py") is None
    assert locate_skill_script("data.csv") is None      # not a script
    assert locate_skill_script("") is None
    print("OK the_locator_matches_by_tail_not_by_guess")


def main() -> None:
    test_the_verbatim_live_failure_is_explained()
    test_a_bare_script_name_also_resolves()
    test_the_node_runner_is_covered_too()
    test_every_launchable_extension_is_redirectable()
    test_an_ordinary_failure_is_left_alone()
    test_a_succeeding_command_gets_nothing()
    test_an_absolute_path_is_not_hijacked()
    test_the_locator_matches_by_tail_not_by_guess()
    print("\nall skill-script hint tests passed")


if __name__ == "__main__":
    main()
