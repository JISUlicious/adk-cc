"""Shared shell-command parsing for the permission layer.

One quote-aware statement splitter + one segment tokenizer, reused by the
read-only classifier (`readonly.py`), the danger classifier
(`permissions/command_safety.py`), and — via migration — the "Allow always"
broadener (`permissions/broadening.py`). Having one parser is what keeps the
three security gates from disagreeing about what a command actually runs.

Two jobs:
  * `split_statements` — break a command into independent statements on the shell
    operators `&& || | ; &` AND newlines (a background `&` and a `\\n` are real
    statement boundaries the old splitter missed, letting `ls & rm -rf /` /
    `ls\\nrm -rf /` hide a second command).
  * `parse_segment` — tokenize one statement and **peel prefix runners** (`env`,
    `sudo`, `nohup`, `time`, …) so the classifiers see the REAL payload binary,
    not the innocuous-looking wrapper. Redirect targets are pulled from the
    tokens (so a quoted `-m "fix > /dev/sda"` is NOT a redirect).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

# Prefix runners that execute a FOLLOWING command — peel them to reach the payload.
_WRAPPERS = frozenset({
    "env", "nohup", "time", "timeout", "xargs", "watch", "nice", "ionice",
    "stdbuf", "setsid", "command", "builtin", "exec", "then", "do",
})
# Privilege escalators — peel like a wrapper but flag it (raises the tier).
_PRIVILEGE = frozenset({"sudo", "doas", "su"})
# Short flags (of the wrappers above) that consume the NEXT token as their value.
_VALUE_FLAGS = frozenset({
    "-n", "-o", "-u", "-g", "-C", "-P", "-S", "-p", "-D", "-R", "-U", "-s",
    "-k", "-I", "-d", "-E", "-i",
})

# Metachars that, unquoted, mean shell features the naive broadener can't handle.
_DANGER_UNQUOTED = frozenset("()$`<>{}")
_DANGER_DOUBLE_QUOTED = frozenset("$`")

_STATEMENT_SEPS = "&|;\n\r"


def has_unsafe_shell_metachars(segment: str) -> bool:
    """True when the segment has shell metachars a naive rewriter can't handle
    (subshell/expansion/redirect/brace). Quote-aware; True on unbalanced quotes."""
    i, n, state = 0, len(segment), "unquoted"
    while i < n:
        c = segment[i]
        if state == "unquoted":
            if c == "'":
                state = "single"
            elif c == '"':
                state = "double"
            elif c == "\\" and i + 1 < n:
                i += 1
            elif c in _DANGER_UNQUOTED:
                return True
        elif state == "single":
            if c == "'":
                state = "unquoted"
        else:  # double
            if c == '"':
                state = "unquoted"
            elif c == "\\" and i + 1 < n:
                i += 1
            elif c in _DANGER_DOUBLE_QUOTED:
                return True
        i += 1
    return state != "unquoted"


def split_statements(command: str) -> Optional[list[tuple[str, str]]]:
    """Split into [(segment, delimiter_to_next)] on `&& || | ; &` and newlines,
    quote-aware. Last delimiter is "". None on degenerate input (empty,
    leading/trailing separator, unbalanced quotes) — callers then treat the whole
    command as one opaque segment."""
    if not command.strip():
        return None
    pairs: list[tuple[str, str]] = []
    buf: list[str] = []
    i, n, state = 0, len(command), "unquoted"

    def flush(sep: str) -> bool:
        seg = "".join(buf).strip()
        if not seg:
            return False
        pairs.append((seg, sep))
        buf.clear()
        return True

    while i < n:
        c = command[i]
        if state == "unquoted":
            if c == "'":
                state = "single"; buf.append(c)
            elif c == '"':
                state = "double"; buf.append(c)
            elif c == "\\" and i + 1 < n:
                buf.append(c); buf.append(command[i + 1]); i += 1
            elif c == "&":
                nxt = command[i + 1] if i + 1 < n else ""
                if nxt == "&":
                    if not flush("&&"):
                        return None
                    i += 1
                elif nxt == ">":  # &> redirect — not a statement boundary
                    buf.append(c)
                else:
                    if not flush("&"):
                        return None
            elif c == "|":
                if i + 1 < n and command[i + 1] == "|":
                    if not flush("||"):
                        return None
                    i += 1
                else:
                    if not flush("|"):
                        return None
            elif c == ";":
                if not flush(";"):
                    return None
            elif c in "\n\r":
                if not flush("\n"):
                    return None
            else:
                buf.append(c)
        elif state == "single":
            buf.append(c)
            if c == "'":
                state = "unquoted"
        else:  # double
            buf.append(c)
            if c == '"':
                state = "unquoted"
            elif c == "\\" and i + 1 < n:
                buf.append(command[i + 1]); i += 1
        i += 1

    if state != "unquoted":
        return None
    trailing = "".join(buf).strip()
    if not trailing:
        return None
    pairs.append((trailing, ""))
    return pairs


@dataclass
class ParsedSegment:
    binary: Optional[str]                    # basename of the real payload, post-peel
    args: list[str] = field(default_factory=list)
    redirect_targets: list[str] = field(default_factory=list)
    privileged: bool = False                 # sudo/doas/su seen
    wrapped: bool = False                    # any prefix-runner peeled
    ok: bool = True                          # shlex parsed cleanly


_REDIR = re.compile(r"^(?:\d+|&)?>>?(.*)$")


def _extract_redirects(tokens: list[str]) -> list[str]:
    """Redirect targets from `>`, `>>`, `N>`, `&>` tokens. A quoted argument is a
    single shlex token that does not start with a redirect operator, so
    `git commit -m "fix > /dev/sda"` yields NO redirect."""
    out: list[str] = []
    j = 0
    while j < len(tokens):
        t = tokens[j]
        m = _REDIR.match(t)
        if m and (">" in t[:3]):  # token is a redirect operator, not a plain word
            rest = m.group(1)
            if rest:
                out.append(rest)
            elif j + 1 < len(tokens):
                out.append(tokens[j + 1]); j += 1
        j += 1
    return out


def _peel(tokens: list[str]) -> tuple[list[str], bool, bool]:
    """Strip leading VAR=val assignments and prefix runners so tokens[0] is the
    real payload binary. Returns (tokens, privileged, wrapped). Best-effort:
    curated value-flags and `timeout`'s duration are skipped; exotic
    wrapper+flag forms may mis-peel (then the payload falls to 'mutating', still
    gated — the OS sandbox is the airtight boundary)."""
    privileged = wrapped = False
    changed = True
    while tokens and changed:
        changed = False
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]; changed = True
        if not tokens:
            break
        b = tokens[0].rsplit("/", 1)[-1]
        if b in _PRIVILEGE or b in _WRAPPERS:
            wrapped = True
            if b in _PRIVILEGE:
                privileged = True
            rest = tokens[1:]
            # skip this runner's own flags (and value-flags' values).
            j = 0
            while j < len(rest) and rest[j].startswith("-"):
                takes_value = rest[j] in _VALUE_FLAGS
                j += 1
                if takes_value and j < len(rest):
                    j += 1
            if b == "timeout" and j < len(rest) and not rest[j].startswith("-"):
                j += 1  # the DURATION argument
            tokens = rest[j:]
            changed = True
    return tokens, privileged, wrapped


def parse_segment(seg: str) -> ParsedSegment:
    """Tokenize one statement, peel prefix runners, extract redirects."""
    seg = seg.strip()
    if not seg:
        return ParsedSegment(binary=None, ok=True)
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        return ParsedSegment(binary=None, ok=False)
    redirects = _extract_redirects(toks)
    payload, privileged, wrapped = _peel(toks)
    if not payload:
        return ParsedSegment(binary=None, redirect_targets=redirects,
                             privileged=privileged, wrapped=wrapped, ok=True)
    return ParsedSegment(
        binary=payload[0].rsplit("/", 1)[-1],
        args=payload[1:],
        redirect_targets=redirects,
        privileged=privileged,
        wrapped=wrapped,
        ok=True,
    )
