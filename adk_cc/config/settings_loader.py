"""Load PermissionRules from a YAML file.

Format:

    rules:
      - tool: run_bash
        behavior: deny
        content: "rm *"
        source: policy
      - tool: write_file
        behavior: ask
        content: "/etc/*"
        source: user
      - tool: "*"
        behavior: allow
        source: project

`source` defaults to `policy`. Missing `content` means the rule applies
to any args for that tool.

PyYAML is loaded lazily so adk-cc remains importable without it. The
loader is used by Stage G's deployment path; dev installs don't need it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..permissions.rules import PermissionRule, RuleBehavior, RuleSource
from ..permissions.settings import SettingsHierarchy


def load_settings_from_yaml(path: str | Path) -> SettingsHierarchy:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required to load YAML settings. "
            "Install with `pip install pyyaml`."
        ) from e

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rules_raw = raw.get("rules", []) or []
    rules = [_parse_rule(r) for r in rules_raw]
    return SettingsHierarchy(rules)


def _parse_rule(d: dict[str, Any]) -> PermissionRule:
    return PermissionRule(
        source=RuleSource(d.get("source", "policy")),
        behavior=RuleBehavior(d["behavior"]),
        tool_name=str(d.get("tool", "*")),
        rule_content=d.get("content"),
    )
