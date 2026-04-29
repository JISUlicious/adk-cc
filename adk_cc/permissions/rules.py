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
from enum import Enum
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


def rule_matches(rule: PermissionRule, tool_name: str, args: dict) -> bool:
    """True if the rule applies to this (tool_name, args) pair."""
    if rule.tool_name != "*" and rule.tool_name != tool_name:
        return False
    if rule.rule_content is None:
        return True
    extractor = _RULE_KEY_EXTRACTORS.get(tool_name)
    if extractor is None:
        # Unknown tool — only "*" rules with no content match it.
        return False
    return fnmatch.fnmatch(extractor(args), rule.rule_content)
