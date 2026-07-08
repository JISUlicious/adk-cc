"""Command safety classifier for run_bash — the shell-command analog of the
protected-path floor (`permissions/protected.py`).

    classify_command(command) -> "read_only" | "mutating" | "dangerous" | "catastrophic"

The tiers drive engine gating (see `engine._decide_impl`): read-only auto-allows
in every mode; mutating keeps today's destructive ask + broadening; dangerous
always asks — even under `bypassPermissions`; catastrophic hard-denies even
under bypass (an explicit operator ALLOW rule can still override both).

All tokenization goes through the shared `tools/bash/parse.py`, so this gate,
the read-only classifier (`readonly.py`), and the broadener agree on what a
command runs — and, crucially, prefix runners are PEELED (`env rm -rf /`,
`sudo rm -rf /`, `nohup rm -rf /` are classified by their real payload, not the
innocuous wrapper). Redirect danger reads the parsed redirect TARGETS (so a
quoted `-m "…/dev/sda…"` is not a redirect, and `2>/dev/null` is benign).

Config:
  ADK_CC_CMD_SAFETY=0           disable — everything -> "mutating" (today's behavior)
  ADK_CC_DANGEROUS_CMDS=a,b     extra dangerous binary basenames
  ADK_CC_CATASTROPHIC_CMDS=a,b  extra catastrophic binary basenames
"""

from __future__ import annotations

import os
import re

from ..tools.bash.parse import ParsedSegment, parse_segment, split_statements
from ..tools.bash.readonly import is_read_only_command

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
    "dd", "shred", "eval", "crontab", "launchctl", "systemctl",
    "kill", "pkill", "killall", "fdisk", "parted", "chattr",
})
# Binaries catastrophic on sight (destructive with no benign form worth the risk).
_CATASTROPHIC_BINS = frozenset({
    "wipefs", "shutdown", "reboot", "halt", "poweroff",
})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})

# `rm -rf` targets that wipe the world.
_ROOT_TARGETS = frozenset({"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "/."})

# Redirect-target device / path classification.
_CATASTROPHIC_DEVICE = re.compile(r"^/dev/(?:sd|disk|nvme|hd|mapper|vd|sr|md)")
_BENIGN_SINKS = frozenset({
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty",
    "/dev/zero", "/dev/random", "/dev/urandom",
})
_DANGEROUS_WRITE_DIR = re.compile(r"^/(?:dev|etc|usr|bin|sbin|boot|sys|proc|System)(?:/|$)")


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


def _positionals(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _redirect_tier(targets: list[str]) -> str | None:
    """Danger from redirect targets: writing a raw block device is catastrophic;
    writing under a system dir (or a non-benign /dev node) is dangerous; the
    common sinks (/dev/null, /dev/stderr, …) are benign."""
    tier = None
    for t in targets:
        if _CATASTROPHIC_DEVICE.match(t):
            return "catastrophic"
        if t in _BENIGN_SINKS or t.startswith("/dev/fd/"):
            continue
        if _DANGEROUS_WRITE_DIR.match(t):
            tier = "dangerous"
    return tier


def _classify_parsed(p: ParsedSegment) -> str:
    if not p.ok:
        return "mutating"  # unparseable → let the normal destructive gate handle it
    tier = "dangerous" if p.privileged else "mutating"  # sudo/doas/su ≥ dangerous
    rt = _redirect_tier(p.redirect_targets)
    if rt:
        tier = _worse(tier, rt)

    b = p.binary
    if b is None:
        return tier
    args = p.args
    letters = _flags(args)
    pos = _positionals(args)
    recursive = "r" in letters.lower() or "--recursive" in args
    force = "f" in letters.lower() or "--force" in args

    # --- catastrophic ---
    if b.startswith("mkfs") or b in _CATASTROPHIC_BINS or b in _env_bins("ADK_CC_CATASTROPHIC_CMDS"):
        return "catastrophic"
    if b == "rm" and "--no-preserve-root" in args:
        return "catastrophic"
    if b == "rm" and recursive and force and any(x in _ROOT_TARGETS for x in pos):
        return "catastrophic"
    if b == "dd" and any(a.startswith("of=/dev/") for a in args):
        return "catastrophic"
    if b in ("chmod", "chown") and ("R" in letters or "--recursive" in args) and any(x == "/" for x in pos):
        return "catastrophic"

    # --- dangerous ---
    if b in _DANGEROUS_BINS or b in _env_bins("ADK_CC_DANGEROUS_CMDS"):
        return _worse(tier, "dangerous")
    if b == "rm" and recursive:  # any recursive rm (root cases already catastrophic)
        return _worse(tier, "dangerous")
    if b in _SHELLS and not pos:  # bare shell reading stdin (the `sh` in `curl … | sh`)
        return _worse(tier, "dangerous")
    if b == "chmod" and ("R" in letters or "--recursive" in args or any("777" in x for x in pos)):
        return _worse(tier, "dangerous")
    if b == "chown" and ("R" in letters or "--recursive" in args):
        return _worse(tier, "dangerous")
    if b == "git" and (
        ("push" in args and ("f" in letters or "--force" in args))
        or ("reset" in args and "--hard" in args)
    ):
        return _worse(tier, "dangerous")

    return tier


def classify_command(command: str) -> str:
    """Tier a shell command. See module docstring."""
    if not _enabled():
        return "mutating"
    command = (command or "").strip()
    if not command:
        return "mutating"
    if is_read_only_command(command):
        return "read_only"
    if _fork_bomb(command):
        return "catastrophic"

    stmts = split_statements(command)
    segments = [command] if stmts is None else [seg for seg, _sep in stmts]
    worst = "mutating"
    for seg in segments:
        worst = _worse(worst, _classify_parsed(parse_segment(seg)))
        if worst == "catastrophic":
            return "catastrophic"
    if worst == "dangerous":
        return "dangerous"

    # No danger found. A cleanly-split pipeline whose every statement is
    # individually read-only (e.g. `git log | head`, `cat a | grep b`) is
    # read-only overall — each is re-checked against the conservative allow-list,
    # so a writer like `… | tee f` keeps it "mutating".
    if stmts is not None and all(is_read_only_command(s) for s, _sep in stmts):
        return "read_only"
    return "mutating"


def command_paths(command: str) -> list[str]:
    """Path-like tokens a command references, for the run_bash protected-path
    floor (engine._decide_impl). Best-effort: shell expansion (`$HOME`, globs
    the shell resolves) can hide a path — the OS sandbox is the airtight
    boundary. Returns the raw tokens; the caller resolves + classifies them."""
    out: list[str] = []
    stmts = split_statements(command)
    segments = [command] if stmts is None else [seg for seg, _sep in stmts]
    for seg in segments:
        p = parse_segment(seg)
        for tok in (*p.args, *p.redirect_targets):
            if tok.startswith(("-",)):
                continue
            if "/" in tok or tok.startswith("~") or tok in (".", ".."):
                out.append(tok)
    return out
