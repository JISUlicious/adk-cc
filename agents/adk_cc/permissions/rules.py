"""Permission rules and per-tool matching.

A rule is a tuple of (source, behavior, tool_name, rule_content?).
- `tool_name` is either a tool's name (e.g. "run_bash") or "*" for any tool.
- `rule_content` is an fnmatch pattern matched against the tool's "rule key"
  (see `_RULE_KEY_EXTRACTORS` below). If `None`, the rule matches any args.

The "rule key" is the single string most operators want to write rules
against:
  - run_bash       → the `command` arg
  - read_file etc. → the `path` arg
  - glob_files     → the `root` arg
  - grep           → the `path` arg

Example rules:
  PermissionRule(source=POLICY, behavior=DENY, tool_name="run_bash", rule_content="rm *")
  PermissionRule(source=USER,   behavior=ASK,  tool_name="write_file", rule_content="/etc/*")
  PermissionRule(source=USER,   behavior=DENY, tool_name="*",         rule_content="/secret/*")
"""

from __future__ import annotations

import fnmatch
import os
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel


class RuleBehavior(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class RuleSource(str, Enum):
    """Layered settings sources, highest priority first."""

    POLICY = "policy"     # operator-managed, can't be overridden by tenant
    USER = "user"         # ~/.adk-cc/permissions
    PROJECT = "project"   # ./.adk-cc/permissions
    SESSION = "session"   # added at runtime


class PermissionRule(BaseModel):
    source: RuleSource
    behavior: RuleBehavior
    tool_name: str        # exact match or "*"
    rule_content: Optional[str] = None  # fnmatch pattern, or None for any args


# Per-tool extractor: given args, return the string that rule_content matches.
_RULE_KEY_EXTRACTORS: dict[str, Callable[[dict], str]] = {
    "read_file":   lambda args: args.get("path", ""),
    "write_file":  lambda args: args.get("path", ""),
    "edit_file":   lambda args: args.get("path", ""),
    "glob_files":  lambda args: args.get("root", "."),
    "grep":        lambda args: args.get("path", "."),
    "run_bash":    lambda args: args.get("command", ""),
}

# Tools whose rule key is a filesystem path — these are matched
# workspace-aware (see `rule_matches`): the raw key OR its
# workspace-resolved absolute form. That lets a `<root>/*` rule (written
# by workspace-anchored "Allow always") match whether the model passed a
# relative (`src/a.ts`) or absolute (`/abs/root/src/a.ts`) path. run_bash
# is excluded — its key is a command, not a path.
_PATH_TOOLS = frozenset(_RULE_KEY_EXTRACTORS) - {"run_bash"}


def _resolve_against_workspace(raw: str, workspace_root: Optional[str]) -> Optional[str]:
    """Canonical absolute form of a path arg: absolute paths resolve as-is,
    relatives anchor under `workspace_root` (mirroring `tools/_fs.resolve`).
    Returns None if `raw` is empty or resolution raises — the caller then
    falls back to raw-only matching. Uses realpath (no existence required)
    so it lines up with the already-canonical `WorkspaceRoot.abs_path`."""
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return os.path.realpath(str(p))
        if workspace_root:
            return os.path.realpath(str(Path(workspace_root) / p))
        return os.path.realpath(str(p))
    except Exception:
        return None


def rule_matches(
    rule: PermissionRule,
    tool_name: str,
    args: dict,
    workspace_root: Optional[str] = None,
) -> bool:
    """True if the rule applies to this (tool_name, args) pair.

    For path tools the match is workspace-aware: the raw key OR its
    workspace-resolved absolute form is tested against `rule_content`.
    This is purely additive — it never drops a match the raw key already
    produced, so existing relative/suffix patterns keep working — but it
    lets a workspace-anchored `<root>/*` rule match a relative path arg.
    """
    if rule.tool_name != "*" and rule.tool_name != tool_name:
        return False
    if rule.rule_content is None:
        return True
    extractor = _RULE_KEY_EXTRACTORS.get(tool_name)
    if extractor is None:
        # Unknown tool — only "*" rules with no content match it.
        return False
    key = extractor(args)
    if fnmatch.fnmatch(key, rule.rule_content):
        return True
    if tool_name in _PATH_TOOLS:
        resolved = _resolve_against_workspace(key, workspace_root)
        if resolved is not None and fnmatch.fnmatch(resolved, rule.rule_content):
            return True
    return False
