"""Command safety classifier for run_bash — the shell-command analog of the
protected-path floor (`permissions/protected.py`).

    classify_command(command) -> "read_only" | "mutating" | "dangerous" | "catastrophic"

The tiers drive engine gating (see `engine._decide_impl`): read-only auto-allows
in every mode; mutating keeps today's destructive ask + broadening; dangerous
always asks — even under `bypassPermissions`; catastrophic hard-denies even
under bypass (an explicit operator ALLOW rule can still override both).

Design (matches how `readonly.py` and `broadening.py` already reason about
commands, and reuses them):
  - read-only is delegated verbatim to `is_read_only_command` (a conservative
    allow-list that already rejects compound/pipe/redirect/subshell).
  - Otherwise the command is split into segments with `_split_compound`
    (quote-aware `&& || | ;` splitter), each segment tokenized with shlex and
    its binary basename-normalized (so `/bin/rm` matches `rm`), and the command
    tier is the MOST SEVERE segment. Cross-cutting shapes (pipe-into-shell,
    redirect-to-device, fork-bomb) are matched on the whole command.
  - Bias to caution: anything we cannot parse is at most "mutating" (never
    "read_only"), so the normal destructive gate still fires.

Config:
  ADK_CC_CMD_SAFETY=0           disable — everything -> "mutating" (today's behavior)
  ADK_CC_DANGEROUS_CMDS=a,b     extra dangerous binary basenames
  ADK_CC_CATASTROPHIC_CMDS=a,b  extra catastrophic binary basenames
"""

from __future__ import annotations

import os
import re
import shlex

from ..tools.bash.readonly import is_read_only_command
from .broadening import _split_compound

_SEVERITY = {"read_only": 0, "mutating": 1, "dangerous": 2, "catastrophic": 3}


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _enabled() -> bool:
    return os.environ.get("ADK_CC_CMD_SAFETY", "1") != "0"


def _env_bins(var: str) -> set[str]:
    raw = os.environ.get(var, "") or ""
    return {p.strip() for p in raw.replace(":", ",").split(",") if p.strip()}


# Binaries dangerous regardless of args.
_DANGEROUS_BINS = frozenset({
    "sudo", "doas", "su", "dd", "shred", "eval", "crontab", "launchctl",
    "systemctl", "kill", "pkill", "killall",
})
# Binaries catastrophic on sight.
_CATASTROPHIC_BINS = frozenset({
    "wipefs", "shutdown", "reboot", "halt", "poweroff", "fdisk", "parted",
})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

# Whole-command shapes.
_PIPE_TO_SHELL = re.compile(r"\|\s*(?:sudo\s+|env\s+\S+\s+)?(?:sh|bash|zsh|dash|ksh)\b")
_CATASTROPHIC_REDIRECT = re.compile(r">\s*/dev/(?:sd|disk|nvme|hd|mapper)")
_DANGEROUS_REDIRECT = re.compile(r">\s*/(?:dev|etc|usr|bin|sbin|boot|sys|proc|System)\b")

# `rm -rf` targets that wipe the world.
_ROOT_TARGETS = frozenset({"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "/."})


def _fork_bomb(command: str) -> bool:
    c = re.sub(r"\s+", "", command)
    return ":(){" in c and (":|:" in c or "|:&" in c or ":|:&" in c)


def _flags(args: list[str]) -> str:
    """Concatenated single-dash flag letters (so `-rf` and `-r -f` look alike)."""
    out = []
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            out.append(a[1:])
    return "".join(out)


def _has_long(args: list[str], name: str) -> bool:
    return name in args


def _positionals(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _classify_segment(seg: str) -> str:
    seg = seg.strip()
    if not seg:
        return "mutating"
    if _CATASTROPHIC_REDIRECT.search(seg):
        return "catastrophic"
    if _DANGEROUS_REDIRECT.search(seg):
        return "dangerous"
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        return "mutating"  # unparseable → let the normal destructive gate handle it
    # Skip leading VAR=val assignments to reach the real binary.
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):
        toks = toks[1:]
    if not toks:
        return "mutating"
    binary = toks[0].rsplit("/", 1)[-1]
    args = toks[1:]
    letters = _flags(args)
    pos = _positionals(args)

    # --- catastrophic ---
    if binary.startswith("mkfs") or binary in _CATASTROPHIC_BINS or binary in _env_bins("ADK_CC_CATASTROPHIC_CMDS"):
        return "catastrophic"
    if binary == "rm" and "r" in letters.lower() and "f" in letters.lower() and any(p in _ROOT_TARGETS for p in pos):
        return "catastrophic"
    if binary == "rm" and ("--no-preserve-root" in args):
        return "catastrophic"
    if binary == "dd" and any(a.startswith("of=/dev/") for a in args):
        return "catastrophic"
    if binary in ("chmod", "chown") and "R" in letters and any(p == "/" for p in pos):
        return "catastrophic"

    # --- dangerous ---
    if binary in _DANGEROUS_BINS or binary in _env_bins("ADK_CC_DANGEROUS_CMDS"):
        return "dangerous"
    if binary == "rm" and ("r" in letters.lower() or "--recursive" in args):
        return "dangerous"  # any recursive rm (root cases already catastrophic)
    if binary in _SHELLS and not pos:
        return "dangerous"  # bare shell reading stdin (e.g. the `sh` in `curl … | sh`)
    if binary == "chmod" and ("R" in letters or any("777" in p for p in pos)):
        return "dangerous"
    if binary == "chown" and "R" in letters:
        return "dangerous"
    if binary == "git" and (
        ("push" in args and ("f" in letters or "--force" in args))
        or ("reset" in args and "--hard" in args)
    ):
        return "dangerous"
    if binary in ("chattr",) and any(a.startswith("-i") or a == "+i" for a in args):
        return "dangerous"

    return "mutating"


def classify_command(command: str) -> str:
    """Tier a shell command. See module docstring."""
    if not _enabled():
        return "mutating"
    command = (command or "").strip()
    if not command:
        return "mutating"
    if is_read_only_command(command):
        return "read_only"

    # Whole-command shapes first.
    if _fork_bomb(command) or _CATASTROPHIC_REDIRECT.search(command):
        return "catastrophic"
    worst = "dangerous" if _PIPE_TO_SHELL.search(command) else "mutating"

    pairs = _split_compound(command)
    segments = [command] if pairs is None else [seg for seg, _sep in pairs]
    for seg in segments:
        worst = _worse(worst, _classify_segment(seg))
        if worst == "catastrophic":
            return "catastrophic"
    if worst == "dangerous":
        return "dangerous"

    # No danger found. A cleanly-split pipeline whose every segment is
    # individually read-only (e.g. `git log | head`, `cat a | grep b`) is
    # read-only overall — each segment is re-checked against the conservative
    # allow-list, so a writer like `… | tee f` keeps it "mutating".
    if pairs is not None and all(is_read_only_command(s) for s, _sep in pairs):
        return "read_only"
    return "mutating"
