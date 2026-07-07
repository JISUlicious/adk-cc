"""Compute broadened `rule_content` patterns for "Allow always".

When the operator clicks Allow always on a `run_bash` invocation, the
naive "literal command" rule re-prompts the moment any arg changes
(e.g. `pip install pandas` → `pip install numpy`). This module
computes a broadened pattern that covers the same command family.

Design
------

Per-binary prefix table (`_RUN_BASH_PREFIX_TOKENS`) — how many leading
tokens belong to the "command identity":

  * Single-binary CLIs (`ls`, `cat`, `python`) → 1 token, broadened
    to `<binary> *`.
  * Subcommand-style CLIs (`pip install ...`, `git status ...`,
    `uv run ...`) → 2 tokens, broadened to `<binary> <subcmd> *`.

Unknown binaries fall back to **2 tokens** — slightly safer than 1
(narrower blast radius if the binary turns out to be a subcommand-
style CLI we forgot to list).

Scope-preserving binaries (`_SCOPE_PRESERVING_BINARIES`) — `cd`,
`source`, `env`, etc. The model often emits compounds like
`cd /home/user/prj && python3 -c "..."` where the cd path is
identical across calls and the operator wants to keep approval
scoped to THAT directory. So scope-preserving binaries keep their
entire segment literal in the broadened compound: the example above
broadens to `cd /home/user/prj && python3 *`, not `cd * && python3 *`.
A subsequent `cd /etc && python3 -c "..."` still re-prompts.

Compound commands (`a && b`, `a | b`, `a; b`, `a || b`) are split on
shell operators (quote-aware), each segment broadened independently
per its binary's prefix / scope-preservation rules, then rejoined
with the original delimiters.

Quote-aware splitting and metachar checks are state-machine based:
operator code like `python3 -c "print(1)"` (parens inside double
quotes) is recognized as quoted-content and does NOT trigger the
subshell-bailout. But `echo "$(date)"` (parameter expansion inside
double quotes — which the shell DOES expand) still bails out, because
double-quoted `$()` is unsafe to broaden naively.

For path-based tools (`read_file`, `write_file`, `edit_file`, …) the
broadened pattern is **workspace-anchored**: when the target resolves
inside the session's workspace root, "Allow always" stores `<root>/*`
so one approval covers the whole bound project (in desktop mode the
workspace root IS the project). fnmatch's `*` spans `/`, so a single
`<root>/*` pattern matches the entire tree, and `rules.rule_matches`
resolves relative path args against the workspace before matching so a
later `edit_file("src/b.ts")` is covered too. Targets OUTSIDE the
workspace (or when no workspace root is known) stay literal — there's
nothing safe to anchor to.

Output
------

`compute_allow_always_rule_contents` returns a list of strings to
store as separate `PermissionRule.rule_content` values. For a
broadenable `run_bash` command, or a path tool inside the workspace,
the list has TWO entries:

  1. The literal key (catches the exact re-run case — fnmatch's
     trailing-space-then-`*` pattern doesn't match the no-args form).
  2. The broadened pattern (`<binary> <subcmd> *` / `<root>/*`).

For out-of-workspace path tools and for malformed commands the list
has ONE entry (literal only).
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Optional

# Per-binary prefix-token count for `run_bash` broadening. Easy to
# extend — add an entry when a new subcommand-style CLI shows up in
# practice. Defaults to 2 (subcommand-style) for binaries not listed
# — conservative because a 2-token prefix has a narrower blast radius
# than 1-token if the binary turns out to be `git`-style.
_RUN_BASH_PREFIX_TOKENS: dict[str, int] = {
    # Single-binary CLIs (1 token).
    # Coreutils / file ops — operator who allows `cd foo` almost
    # always wants `cd <any-dir>` too; same for the others.
    "cd":      1,
    "pwd":     1,
    "ls":      1,
    "cat":     1,
    "echo":    1,
    "printf":  1,
    "mkdir":   1,
    "rmdir":   1,
    "touch":   1,
    "rm":      1,
    "cp":      1,
    "mv":      1,
    "ln":      1,
    "chmod":   1,
    "chown":   1,
    "tee":     1,
    "xargs":   1,
    "find":    1,
    "rg":      1,
    "grep":    1,
    "head":    1,
    "tail":    1,
    "wc":      1,
    "sort":    1,
    "uniq":    1,
    "cut":     1,
    "tr":      1,
    "sed":     1,
    "awk":     1,
    "tree":    1,
    "which":   1,
    "whereis": 1,
    "ps":      1,
    "df":      1,
    "du":      1,
    "stat":    1,
    "file":    1,
    "env":     1,
    "sleep":   1,
    "basename": 1,
    "dirname": 1,
    # Interpreters / test runners.
    "pytest":  1,
    "python":  1,
    "python3": 1,
    "node":    1,
    "ruby":    1,
    "go":      1,  # `go test`, `go build` — debatable; keep 1 for now
    # Subcommand-style CLIs (2 tokens).
    "pip":     2,
    "uv":      2,
    "git":     2,
    "npm":     2,
    "yarn":    2,
    "pnpm":    2,
    "cargo":   2,
    "kubectl": 2,
    "docker":  2,
    "make":    2,
    "brew":    2,
    "apt":     2,
    "gh":      2,
    "aws":     2,
    "gcloud":  2,
    "az":      2,
}
_DEFAULT_PREFIX_TOKENS = 2

# Binaries whose segment stays literal in the broadened compound.
# Rationale: model-emitted compounds usually pin the working
# directory or environment with these (`cd /path && cmd ...`,
# `source venv/bin/activate && python ...`) — and the operator
# clicking Allow always typically intends "this exact directory /
# this exact env" + "any args to the following command". Broadening
# their args would silently widen scope to other directories /
# environments.
_SCOPE_PRESERVING_BINARIES = frozenset({
    "cd",
    "source",
    ".",        # POSIX sh source operator
    "pushd",
    "popd",
    "export",
    "env",
    "exec",
})

# Metachars whose presence outside of quotes signals shell features
# the naive broadener can't safely handle:
#   $, `  → command substitution / parameter expansion
#   (, )  → command grouping or subshell
#   {, }  → brace expansion
#   <, >  → I/O redirects
# Inside single quotes EVERYTHING is literal — these are safe.
# Inside double quotes, `$` and backtick still trigger expansion.
_DANGER_UNQUOTED = frozenset("()$`<>{}")
_DANGER_DOUBLE_QUOTED = frozenset("$`")


def compute_allow_always_rule_contents(
    tool_name: str, args: dict, workspace_root: Optional[str] = None
) -> list[str]:
    """Return rule_content strings to store for an Allow always click.

    Returns a list — 2 entries when broadening applies (`run_bash`, or a
    path tool whose target resolves inside `workspace_root`), 1 entry
    otherwise (out-of-workspace path, malformed command, unknown tool).

    Always returns at least one entry (the literal extracted key),
    so callers can write at least one rule per click without
    branching on emptiness.
    """
    extractor_map = _extractors_snapshot()
    extractor = extractor_map.get(tool_name)
    if extractor is None:
        # Unknown tool — no rule_content key to use. The caller's
        # _add_session_allow already handles `None` rule_content
        # (matches any args); reflect that here with an empty list,
        # which the caller can map to a single `rule_content=None`
        # rule.
        return [""]

    raw = extractor(args)
    if not isinstance(raw, str):
        return [""]

    literal = raw.strip()
    if not literal:
        return [""]

    if tool_name == "run_bash":
        broadened = _broaden_run_bash(literal)
        if broadened is None or broadened == literal:
            return [literal]
        return [literal, broadened]

    # Path tools: anchor to the workspace root so one click covers the
    # whole bound project. Only when the target actually resolves inside
    # the workspace — otherwise there's nothing safe to broaden to.
    anchored = _workspace_anchor(literal, workspace_root)
    if anchored is not None and anchored != literal:
        return [literal, anchored]
    return [literal]


def _workspace_anchor(raw_path: str, workspace_root: Optional[str]) -> Optional[str]:
    """`<workspace_root>/*` when `raw_path` resolves inside the workspace,
    else None. Relative paths anchor under the root (mirroring
    `tools/_fs.resolve`); absolute paths must fall under it. Uses realpath
    (no existence required) to line up with the canonical
    `WorkspaceRoot.abs_path`."""
    if not workspace_root:
        return None
    try:
        root = os.path.realpath(workspace_root)
        p = Path(raw_path).expanduser()
        target = os.path.realpath(
            str(p) if p.is_absolute() else str(Path(root) / p)
        )
        if target == root or target.startswith(root + os.sep):
            return f"{root}/*"
    except Exception:
        return None
    return None


def _extractors_snapshot() -> dict:
    """Import-time-late lookup to avoid a circular import (this module
    is imported from the permission engine, which is also where
    `_RULE_KEY_EXTRACTORS` lives)."""
    from .rules import _RULE_KEY_EXTRACTORS
    return _RULE_KEY_EXTRACTORS


def _broaden_run_bash(command: str) -> Optional[str]:
    """Return the broadened pattern for a `run_bash` command, or None
    if broadening would be unsafe / unreliable (caller falls back to
    literal-only)."""
    segments_with_seps = _split_compound(command)
    if not segments_with_seps:
        return None

    broadened_segments: list[str] = []
    for segment, _sep in segments_with_seps:
        if _has_unsafe_shell_metachars(segment):
            return None  # subshells, redirects, expansion — bail
        broadened = _broaden_segment(segment)
        if broadened is None:
            return None
        broadened_segments.append(broadened)

    # Rejoin with original delimiters.
    pieces: list[str] = []
    for (_segment_text, sep), broadened in zip(segments_with_seps, broadened_segments):
        pieces.append(broadened)
        if sep:
            pieces.append(f" {sep} ")
    return "".join(pieces)


def _has_unsafe_shell_metachars(segment: str) -> bool:
    """True when the segment contains shell metachars the naive
    broadener can't handle. State machine: tracks single-quote and
    double-quote regions so user-data parens / braces inside quotes
    (e.g. `python3 -c "print(1)"`) don't trigger a false-positive
    bailout. Returns True on unbalanced quotes too — we can't reason
    about a malformed segment."""
    i = 0
    n = len(segment)
    state = "unquoted"  # "unquoted" | "single" | "double"
    while i < n:
        c = segment[i]
        if state == "unquoted":
            if c == "'":
                state = "single"
            elif c == '"':
                state = "double"
            elif c == "\\" and i + 1 < n:
                i += 1  # consume the escaped char
            elif c in _DANGER_UNQUOTED:
                return True
        elif state == "single":
            # Single quotes are literal — nothing inside expands. Even
            # `$` and `` ` `` are safe. We just need to find the close.
            if c == "'":
                state = "unquoted"
        else:  # state == "double"
            if c == '"':
                state = "unquoted"
            elif c == "\\" and i + 1 < n:
                i += 1
            elif c in _DANGER_DOUBLE_QUOTED:
                return True
        i += 1
    # Unbalanced quote → bail (the splitter would have already failed
    # cleanly in most cases, but defensive).
    return state != "unquoted"


def _split_compound(command: str) -> Optional[list[tuple[str, str]]]:
    """Split a compound command into [(segment, delimiter_to_next)].
    The last segment's delimiter is empty.

    Quote-aware: separators inside single or double quotes are NOT
    treated as segment boundaries. So `echo "a && b"` stays one
    segment. Returns None on degenerate input (empty, leading/trailing
    separator, unbalanced quotes)."""
    if not command.strip():
        return None

    pairs: list[tuple[str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(command)
    state = "unquoted"

    def flush(sep: str) -> bool:
        segment = "".join(buf).strip()
        if not segment:
            return False  # leading separator or empty segment — bail
        pairs.append((segment, sep))
        buf.clear()
        return True

    while i < n:
        c = command[i]
        if state == "unquoted":
            if c == "'":
                state = "single"
                buf.append(c)
            elif c == '"':
                state = "double"
                buf.append(c)
            elif c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(command[i + 1])
                i += 1
            elif c == "&" and i + 1 < n and command[i + 1] == "&":
                if not flush("&&"):
                    return None
                i += 1
            elif c == "|" and i + 1 < n and command[i + 1] == "|":
                if not flush("||"):
                    return None
                i += 1
            elif c == "|":
                if not flush("|"):
                    return None
            elif c == ";":
                if not flush(";"):
                    return None
            else:
                buf.append(c)
        elif state == "single":
            buf.append(c)
            if c == "'":
                state = "unquoted"
        else:  # state == "double"
            buf.append(c)
            if c == '"':
                state = "unquoted"
            elif c == "\\" and i + 1 < n:
                buf.append(command[i + 1])
                i += 1
        i += 1

    if state != "unquoted":
        return None  # unbalanced quote

    trailing = "".join(buf).strip()
    if not trailing:
        # Command ended on a separator (`ls &&`).
        return None
    pairs.append((trailing, ""))
    return pairs


def _broaden_segment(segment: str) -> Optional[str]:
    """Tokenize one segment with shlex, take the first N tokens per
    the per-binary table, append ` *`. Scope-preserving binaries
    (`cd`, `source`, etc.) keep the segment literal — see module
    docstring. Returns None if shlex fails."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    binary = tokens[0]
    # Strip a leading path-component for the binary lookup so
    # `/usr/local/bin/pip install pandas` matches the `pip` entry.
    binary_basename = binary.rsplit("/", 1)[-1]

    if binary_basename in _SCOPE_PRESERVING_BINARIES:
        # Keep the segment fully literal — scope (directory, env, etc.)
        # would silently widen if we broadened. Use the segment text
        # verbatim (already trimmed by `_split_compound`).
        return segment.strip()

    n = _RUN_BASH_PREFIX_TOKENS.get(
        binary_basename, _DEFAULT_PREFIX_TOKENS
    )
    prefix = tokens[: max(1, n)]
    # Re-quote tokens that contain spaces / special chars so the
    # stored pattern is shell-safe to read by a human operator. fnmatch
    # ignores quotes anyway; this is purely for legibility.
    quoted = [shlex.quote(t) if _needs_quoting(t) else t for t in prefix]
    return " ".join(quoted) + " *"


def _needs_quoting(token: str) -> bool:
    """Lightweight check — quote tokens that contain whitespace or
    shell-magic chars when echoed back into a stored rule string."""
    if not token:
        return True
    # Letters / digits / common harmless punctuation = safe unquoted.
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./=:@+,%")
    return any(c not in safe for c in token)
