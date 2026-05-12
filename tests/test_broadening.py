"""Unit tests for `compute_allow_always_rule_contents`.

Covers:
  - Single-command broadening for `run_bash` per the per-binary
    prefix table (1-token vs 2-token CLIs, default fallback).
  - Compound commands split on `&&`, `||`, `|`, `;`.
  - Quote-aware bailout (subshells, redirects, command substitution
    → literal-only).
  - Path tools stay literal in this PR (workspace-scope is a
    follow-up).
  - Unknown tools collapse to a single empty-string entry (caller
    translates to `rule_content=None`, matches any args).
  - End-to-end: a stored broadened rule fnmatches the args-changed
    variants of the original command but not unrelated commands.

Run: `.venv/bin/python tests/test_broadening.py`
"""

from __future__ import annotations

import fnmatch
import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.permissions.broadening import (
    _DEFAULT_PREFIX_TOKENS,
    _RUN_BASH_PREFIX_TOKENS,
    compute_allow_always_rule_contents,
)


# --- Single-command broadening -------------------------------------


def test_subcommand_style_two_tokens() -> None:
    """`pip install pandas` → literal + `pip install *`. Matches the
    user's canonical example. The 2-token prefix lets subsequent
    `pip install requests` auto-allow but `pip uninstall pandas`
    still gates."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "pip install pandas"}
    )
    assert out == ["pip install pandas", "pip install *"], out
    print("OK test_subcommand_style_two_tokens")


def test_subcommand_two_tokens_no_args() -> None:
    """`git status` (2 tokens, no args) → literal + `git status *`.
    The broadened pattern's trailing-space-then-`*` doesn't match the
    no-args form, which is exactly why we store the literal too."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "git status"}
    )
    assert out == ["git status", "git status *"], out
    # Verify the matching behavior end-to-end via fnmatch.
    assert fnmatch.fnmatch("git status -short", "git status *")
    # `git status` literal does NOT match the broadened pattern —
    # that's why the literal entry exists alongside.
    assert not fnmatch.fnmatch("git status", "git status *")
    print("OK test_subcommand_two_tokens_no_args")


def test_single_binary_one_token() -> None:
    """`ls -la /tmp` → literal + `ls *`. The per-binary table marks
    `ls` as 1-token because it's a single-purpose binary; any args
    are negotiable."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "ls -la /tmp"}
    )
    assert out == ["ls -la /tmp", "ls *"], out
    print("OK test_single_binary_one_token")


def test_unknown_binary_defaults_to_two_tokens() -> None:
    """An unfamiliar binary defaults to a 2-token prefix —
    narrower blast radius than 1-token if it turns out to be a
    `git`-style CLI we didn't list. `myThing build foo` →
    `myThing build *`."""
    assert "myThing" not in _RUN_BASH_PREFIX_TOKENS
    assert _DEFAULT_PREFIX_TOKENS == 2  # if this changes, update the test
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "myThing build foo"}
    )
    assert out == ["myThing build foo", "myThing build *"], out
    print("OK test_unknown_binary_defaults_to_two_tokens")


def test_binary_with_path_strips_for_lookup() -> None:
    """`/usr/local/bin/pip install pandas` looks up `pip` (basename)
    in the per-binary table, so the 2-token prefix kicks in."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "/usr/local/bin/pip install pandas"}
    )
    # Quote of the binary path is preserved (it might contain
    # spaces in pathological cases; shlex.quote keeps it shell-safe).
    assert len(out) == 2, out
    assert out[0] == "/usr/local/bin/pip install pandas"
    # The broadened form keeps the full path of the binary token
    # (we only use the basename for the table lookup, not for storage).
    assert out[1] == "/usr/local/bin/pip install *", out[1]
    print("OK test_binary_with_path_strips_for_lookup")


def test_single_token_command_only() -> None:
    """Just `ls` (no args) → literal + `ls *`. The 1-token prefix
    still emits the broadened form so a follow-up `ls /tmp`
    auto-allows."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "ls"}
    )
    assert out == ["ls", "ls *"], out
    print("OK test_single_token_command_only")


# --- Compound commands ---------------------------------------------


def test_compound_and() -> None:
    """`cd foo && pytest tests/x.py` → broaden each segment, rejoin
    with `&&`. Both `cd` and `pytest` are 1-token in the per-binary
    table, so the broadened pattern is `cd * && pytest *` — covers
    `cd bar && pytest other`, blocks `cd bar && rm -rf /`."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "cd foo && pytest tests/x.py"}
    )
    assert len(out) == 2, out
    assert out[0] == "cd foo && pytest tests/x.py"
    assert out[1] == "cd * && pytest *", out[1]
    # Match semantics end-to-end.
    assert fnmatch.fnmatch("cd bar && pytest other", out[1])
    assert fnmatch.fnmatch("cd foo && pytest tests/x.py", out[1])
    assert not fnmatch.fnmatch("cd bar && rm -rf /", out[1])
    print("OK test_compound_and")


def test_compound_pipe() -> None:
    """`ls /tmp | grep foo` → `ls * | grep *` (both 1-token binaries).
    A later `ls /etc | grep bar` auto-allows; `ls /tmp | rm bar` does
    not (the second segment's binary differs)."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "ls /tmp | grep foo"}
    )
    assert out == ["ls /tmp | grep foo", "ls * | grep *"], out
    print("OK test_compound_pipe")


def test_compound_or_and_semicolon() -> None:
    """`||` and `;` are also recognized as segment delimiters.
    `make` is 2-token in the table (so `make build *`); `echo` is
    1-token."""
    out_or = compute_allow_always_rule_contents(
        "run_bash", {"command": "make build || echo failed"}
    )
    assert out_or[1] == "make build * || echo *", out_or[1]

    out_semi = compute_allow_always_rule_contents(
        "run_bash", {"command": "ls; echo done"}
    )
    assert out_semi[1] == "ls * ; echo *", out_semi[1]
    print("OK test_compound_or_and_semicolon")


# --- Bailout cases (literal-only) ----------------------------------


def test_subshell_bails_to_literal() -> None:
    """Command substitution (`$(...)`, backticks) is a sign of more
    complex shell parsing than our naive splitter handles. Bail to
    literal to avoid mis-broadening."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "echo $(date)"}
    )
    assert out == ["echo $(date)"], out
    out_bt = compute_allow_always_rule_contents(
        "run_bash", {"command": "echo `whoami`"}
    )
    assert out_bt == ["echo `whoami`"], out_bt
    print("OK test_subshell_bails_to_literal")


def test_redirect_bails_to_literal() -> None:
    """Redirects (`>`, `<`) trigger the suspicious-char bailout."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "echo hi > /tmp/out"}
    )
    assert out == ["echo hi > /tmp/out"], out
    print("OK test_redirect_bails_to_literal")


def test_brace_or_paren_bails_to_literal() -> None:
    """Brace expansion / grouping — same naive-splitter risk."""
    out_brace = compute_allow_always_rule_contents(
        "run_bash", {"command": "cp file.{txt,bak} /tmp"}
    )
    assert out_brace == ["cp file.{txt,bak} /tmp"], out_brace

    out_paren = compute_allow_always_rule_contents(
        "run_bash", {"command": "(cd foo && pytest)"}
    )
    assert out_paren == ["(cd foo && pytest)"], out_paren
    print("OK test_brace_or_paren_bails_to_literal")


def test_unbalanced_quotes_bail_to_literal() -> None:
    """A segment that shlex can't tokenize (e.g. unbalanced quote)
    makes the whole command fall back to literal — no partial
    broadening is safer than the wrong broadening."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": 'echo "unterminated'}
    )
    assert out == ['echo "unterminated'], out
    print("OK test_unbalanced_quotes_bail_to_literal")


# --- Path tools (literal in this PR) -------------------------------


def test_path_tools_stay_literal() -> None:
    """`read_file`/`write_file`/`edit_file`/`grep`/`glob_files` return
    only the literal path. Workspace-anchored broadening is a
    separate follow-up PR; until then, paths are exact-match."""
    for tool, key in (
        ("read_file", "path"),
        ("write_file", "path"),
        ("edit_file", "path"),
        ("grep", "path"),
        ("glob_files", "root"),
    ):
        out = compute_allow_always_rule_contents(tool, {key: "/workspace/foo.py"})
        assert out == ["/workspace/foo.py"], (tool, out)
    print("OK test_path_tools_stay_literal")


# --- Edge cases ----------------------------------------------------


def test_unknown_tool_returns_empty_string() -> None:
    """Unknown tool → single empty-string entry. The caller
    (`_add_session_allow`) translates this to `rule_content=None`,
    which the engine treats as "matches any args for that tool"."""
    out = compute_allow_always_rule_contents("some_custom_tool", {"foo": "bar"})
    assert out == [""], out
    print("OK test_unknown_tool_returns_empty_string")


def test_empty_command_returns_empty() -> None:
    """Empty/whitespace command also collapses to the empty-content
    fallback — the caller writes a single rule with rule_content=None."""
    for raw in ("", "   ", "\t\n"):
        out = compute_allow_always_rule_contents(
            "run_bash", {"command": raw}
        )
        assert out == [""], (raw, out)
    print("OK test_empty_command_returns_empty")


def test_non_string_command_returns_empty() -> None:
    """A bogus `command` value (None, int) collapses to empty."""
    out_none = compute_allow_always_rule_contents("run_bash", {"command": None})
    assert out_none == [""], out_none
    out_int = compute_allow_always_rule_contents("run_bash", {"command": 42})
    assert out_int == [""], out_int
    print("OK test_non_string_command_returns_empty")


# --- End-to-end fnmatch semantics ----------------------------------


def test_pip_install_pattern_covers_args_variations() -> None:
    """The whole point: after Allow always on `pip install pandas`,
    the engine's `rule_matches` lets `pip install numpy` through but
    NOT `pip uninstall pandas` or `git status`."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "pip install pandas"}
    )
    pattern = out[1]  # the broadened entry
    assert fnmatch.fnmatch("pip install pandas", "pip install pandas")  # literal
    assert fnmatch.fnmatch("pip install numpy", pattern)
    assert fnmatch.fnmatch("pip install -e .", pattern)
    assert not fnmatch.fnmatch("pip uninstall pandas", pattern)
    assert not fnmatch.fnmatch("git status", pattern)
    print("OK test_pip_install_pattern_covers_args_variations")


def test_compound_pattern_constrains_per_segment() -> None:
    """Compound broadening's value: each segment's binary must still
    match. Operator who allowed `cd foo && pytest tests` does NOT
    thereby allow `cd foo && rm -rf /` (rm != pytest)."""
    out = compute_allow_always_rule_contents(
        "run_bash", {"command": "cd foo && pytest tests"}
    )
    pattern = out[1]
    assert pattern == "cd * && pytest *", pattern
    assert fnmatch.fnmatch("cd bar && pytest other_dir", pattern)
    assert fnmatch.fnmatch("cd foo && pytest -k slow", pattern)
    assert not fnmatch.fnmatch("cd foo && rm -rf /", pattern)
    assert not fnmatch.fnmatch("cd foo && cat /etc/shadow", pattern)
    print("OK test_compound_pattern_constrains_per_segment")


# --- Driver --------------------------------------------------------


def main() -> None:
    test_subcommand_style_two_tokens()
    test_subcommand_two_tokens_no_args()
    test_single_binary_one_token()
    test_unknown_binary_defaults_to_two_tokens()
    test_binary_with_path_strips_for_lookup()
    test_single_token_command_only()
    test_compound_and()
    test_compound_pipe()
    test_compound_or_and_semicolon()
    test_subshell_bails_to_literal()
    test_redirect_bails_to_literal()
    test_brace_or_paren_bails_to_literal()
    test_unbalanced_quotes_bail_to_literal()
    test_path_tools_stay_literal()
    test_unknown_tool_returns_empty_string()
    test_empty_command_returns_empty()
    test_non_string_command_returns_empty()
    test_pip_install_pattern_covers_args_variations()
    test_compound_pattern_constrains_per_segment()
    print("\nall broadening tests passed")


if __name__ == "__main__":
    main()
