"""Tests for the plan-mode read-only shell classifier.

`is_read_only_command` is a SECURITY BOUNDARY: it only widens what plan mode
permits, so a false positive (a mutating command classified read-only) is a
hole. These tests pin down both directions — the read-only allowlist returns
True, and every write / redirect / chaining / per-program write vector returns
False. Bias-to-False on uncertainty is asserted for unknown programs.

Run: `.venv/bin/python tests/test_bash_readonly.py`
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.tools.bash.readonly import is_read_only_command


# Commands that MUST be classified read-only (allowed in plan mode).
_READ_ONLY = [
    "ls",
    "ls -la",
    "ls -la /some/dir",
    "cat foo.txt",
    "head -n 20 foo.txt",
    "tail -n 5 foo.txt",
    "wc -l foo.txt",
    "grep pattern file.txt",
    "rg TODO",
    "pwd",
    "stat foo.txt",
    "file foo.txt",
    "du -sh .",
    "df -h",
    "echo hello world",
    "which python",
    "printenv",
    "cut -d: -f1 /etc/passwd",
    "column -t data.txt",
    "diff a.txt b.txt",
    "/usr/bin/ls -la",  # absolute path resolves to basename `ls`
    "find . -name '*.py'",
    "find . -type f",
    "sort data.txt",
    "tree",
    "git status",
    "git log --oneline -20",
    "git diff HEAD~1",
    "git show HEAD",
    "git branch -a",
    "git ls-files",
    "git config --get user.name",
    "git config --list",
    "git config --get-regexp '^user'",
]

# Commands that MUST be rejected (mutating, or unclassifiable → deny).
_NOT_READ_ONLY = [
    # obvious writers
    "rm -rf /tmp/x",
    "mv a.txt b.txt",
    "cp a.txt b.txt",
    "mkdir newdir",
    "touch newfile",
    "tee out.txt",
    "sed -i s/a/b/ f.txt",
    # git writers
    "git commit -m msg",
    "git checkout main",
    "git reset --hard",
    "git push origin main",
    "git config user.name foo",  # sets config (no --get/--list)
    # per-program write vectors
    "sort -o out.txt data.txt",
    "sort --output=out.txt data.txt",
    "tree -o out.html",
    "find . -delete",
    "find . -name '*.py' -delete",
    "find . -exec rm {} +",  # -exec (also flagged), and metachar-free
    # shell chaining / redirection / subshells
    "ls; rm y",
    "cat f | tee g",
    "echo x > f",
    "echo x >> f",
    "cat < f",
    "ls `whoami`",
    "ls $(pwd)",
    "ls && rm y",
    "true || rm y",
    # not on the allowlist → deny
    "python script.py",
    "node app.js",
    "sudo ls",
    "unknownprog --flag",
    "npm install",
    # `env`/wrappers run an arbitrary following command → never read-only
    "env",
    "env rm -rf /",
    "env FOO=bar cat x",
    # a newline separates statements — `ls\nrm y` is two commands
    "ls\nrm y",
    # degenerate input
    "",
    "   ",
]


def test_read_only_allowlist_true() -> None:
    for cmd in _READ_ONLY:
        assert is_read_only_command(cmd) is True, f"expected read-only: {cmd!r}"
    print(f"OK test_read_only_allowlist_true ({len(_READ_ONLY)} cmds)")


def test_writers_and_unknown_false() -> None:
    for cmd in _NOT_READ_ONLY:
        assert is_read_only_command(cmd) is False, f"expected NOT read-only: {cmd!r}"
    print(f"OK test_writers_and_unknown_false ({len(_NOT_READ_ONLY)} cmds)")


def test_non_string_inputs_false() -> None:
    for bad in (None, 123, [], {}, object()):
        assert is_read_only_command(bad) is False, f"expected False for {bad!r}"
    print("OK test_non_string_inputs_false")


def test_unbalanced_quotes_false() -> None:
    # shlex.split raises ValueError on unterminated quotes → deny.
    assert is_read_only_command("cat 'unterminated") is False
    print("OK test_unbalanced_quotes_false")


def main() -> None:
    test_read_only_allowlist_true()
    test_writers_and_unknown_false()
    test_non_string_inputs_false()
    test_unbalanced_quotes_false()
    print("\nall bash read-only classifier tests passed")


if __name__ == "__main__":
    main()
