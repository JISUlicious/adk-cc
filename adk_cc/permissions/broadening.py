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

Compound commands (`a && b`, `a | b`, `a; b`, `a || b`) are split on
shell operators, each segment broadened independently, then rejoined
with the original delimiters. So `cd foo && pytest tests` becomes
`cd foo * && pytest *` — covers `cd other && pytest different`, but
NOT `cd other && rm -rf /` (the second segment's first token must
still match).

Quote-aware splitting is intentionally NOT attempted: any segment
that fails `shlex.split` makes the whole command fall back to a
literal-only rule (no broadening). The cost is a re-prompt on a
quoted-pipe command; the alternative is a regex that mis-splits
`echo "a && b"` and broadens the wrong thing.

For path-based tools (`read_file`, `write_file`, etc.) we currently
return the literal path only — workspace-anchored broadening is a
separate design problem tracked for a follow-up PR.

Output
------

`compute_allow_always_rule_contents` returns a list of strings to
store as separate `PermissionRule.rule_content` values. For a
broadenable `run_bash` command the list has TWO entries:

  1. The literal command (catches the exact re-run case — fnmatch's
     trailing-space-then-`*` pattern doesn't match the no-args form).
  2. The broadened pattern (covers args-only variations).

For path tools and for malformed commands the list has ONE entry
(literal only).
"""

from __future__ import annotations

import re
import shlex
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

# Recognized shell operators. Split on these to handle compound commands.
# Naive: doesn't respect quotes (e.g. `echo "a && b"` splits wrongly);
# segments that fail shlex.split downstream make the whole command
# fall back to literal-only.
_COMPOUND_SEP_RE = re.compile(r"\s*(\|\||&&|;|\|)\s*")

# Shell metachars whose presence in a SEGMENT (post-split) is a sign
# of further compound-ness we didn't catch. Used to bail out to
# literal — same fail-safe reasoning as the shlex check.
_SUSPICIOUS_SEGMENT_CHARS = frozenset("$`(){}<>")


def compute_allow_always_rule_contents(tool_name: str, args: dict) -> list[str]:
    """Return rule_content strings to store for an Allow always click.

    Returns a list — usually 2 entries for `run_bash` (literal +
    broadened), 1 entry for path tools or for malformed `run_bash`
    commands.

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

    # Path tools stay literal in this PR. Workspace-anchored
    # broadening is its own design (Phase 2 PR).
    if tool_name != "run_bash":
        return [literal]

    broadened = _broaden_run_bash(literal)
    if broadened is None or broadened == literal:
        return [literal]
    return [literal, broadened]


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
        if any(c in segment for c in _SUSPICIOUS_SEGMENT_CHARS):
            return None  # subshells, redirects, command substitution — bail
        broadened = _broaden_segment(segment)
        if broadened is None:
            return None
        broadened_segments.append(broadened)

    # Rejoin with original delimiters.
    pieces: list[str] = []
    for (segment_text, sep), broadened in zip(segments_with_seps, broadened_segments):
        pieces.append(broadened)
        if sep:
            pieces.append(f" {sep} ")
    return "".join(pieces)


def _split_compound(command: str) -> Optional[list[tuple[str, str]]]:
    """Split a compound command into [(segment, delimiter_to_next)].
    The last segment's delimiter is empty. Returns None on a
    degenerate input (e.g. command starts with an operator)."""
    if not command.strip():
        return None
    parts = _COMPOUND_SEP_RE.split(command)
    # `re.split` with a capture group returns: [text, sep, text, sep, ..., text]
    # Always an odd number of elements.
    if len(parts) % 2 == 0:
        return None  # split produced an even count → leading/trailing sep
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(parts), 2):
        segment = parts[i].strip()
        sep = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if not segment:
            return None  # empty segment → operator at boundary; bail
        pairs.append((segment, sep))
    return pairs


def _broaden_segment(segment: str) -> Optional[str]:
    """Tokenize one segment with shlex, take the first N tokens per
    the per-binary table, append ` *`. Returns None if shlex fails
    (the caller bails to literal-only for the whole command)."""
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
