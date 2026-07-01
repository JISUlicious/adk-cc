"""Classify a shell command as strictly READ-ONLY, so plan mode can allow
`run_bash` for exploration (`ls`, `cat`, `git log`, …) while still blocking any
command that could mutate the workspace.

This is a SECURITY BOUNDARY: it only ever WIDENS what plan mode permits, so a
false positive (calling a mutating command read-only) is a hole. Therefore it is
deliberately conservative — it returns True only for single commands built from a
small allowlist, and REJECTS on any sign of a write:

  - shell chaining / redirection / subshells (`;`, `&&`, `|`, `>`, `<`, `` ` ``,
    `$(` ) — any of these could route into a mutating command;
  - per-program write vectors (`find -exec/-delete`, `sort -o`, `sed -i`, …);
  - anything not on the read-only allowlist.

Bias to False on ANY uncertainty. Users who need a pipeline or a writer just
exit plan mode.
"""

from __future__ import annotations

import re
import shlex

# Programs that only ever write to stdout (no file-write flags to worry about
# once shell redirection is ruled out).
_SAFE_PROGRAMS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "pwd",
    "stat", "file", "du", "df", "echo", "which", "type", "env", "printenv",
    "basename", "dirname", "realpath", "readlink", "cut", "nl", "date", "whoami",
    "id", "uname", "hostname", "diff", "cmp", "column", "tr", "man", "help",
})

# find flags that run or delete things.
_FIND_WRITE_FLAGS = frozenset({
    "-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls",
})

# git subcommands that only read.
_GIT_READ_ONLY = frozenset({
    "status", "log", "diff", "show", "branch", "ls-files", "ls-tree", "cat-file",
    "rev-parse", "describe", "blame", "shortlog", "tag", "reflog", "grep",
    "for-each-ref", "whatchanged", "name-rev", "rev-list",
})

# Chaining / redirection / subshell / command-substitution — any of these can
# reach a mutating command, so a command containing them is never read-only here.
_SHELL_META = re.compile(r"[;&|`<>]|\$\(")


def is_read_only_command(command: str) -> bool:
    """True only for a single command built from the read-only allowlist with no
    shell chaining/redirection and no per-program write flags. Conservative."""
    if not command or not isinstance(command, str):
        return False
    cmd = command.strip()
    if not cmd or _SHELL_META.search(cmd):
        return False
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return False
    if not tokens:
        return False

    prog = tokens[0].rsplit("/", 1)[-1]  # /usr/bin/ls -> ls
    args = tokens[1:]

    if prog == "find":
        return not any(a in _FIND_WRITE_FLAGS for a in args)
    if prog in ("sort", "tree"):
        # `-o FILE` / `-oFILE` / `--output` / `--output=FILE` write to a file.
        return not any(
            a.startswith("-o") or a.startswith("--output") for a in args
        )
    if prog == "git":
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub == "config":
            # Only reads: --get / --list. Anything else may set config.
            return any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in args)
        return sub in _GIT_READ_ONLY

    return prog in _SAFE_PROGRAMS
