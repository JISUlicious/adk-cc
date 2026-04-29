"""Layered settings hierarchy that holds permission rules.

For Stage B we only model the in-memory layout. Stage G's `config/
settings_loader.py` will populate this from YAML/env. Sources are stored
in priority order (highest first); within a source, rules are evaluated
in declaration order.
"""

from __future__ import annotations

from typing import Iterable

from .rules import PermissionRule, RuleSource


class SettingsHierarchy:
    """In-memory permission settings.

    Rules are organized per source; iteration order is highest-priority
    first (POLICY → USER → PROJECT → SESSION). Mutations affect only
    the SESSION layer — operator-set policies stay frozen.
    """

    _ORDER = (
        RuleSource.POLICY,
        RuleSource.USER,
        RuleSource.PROJECT,
        RuleSource.SESSION,
    )

    def __init__(self, rules: Iterable[PermissionRule] = ()) -> None:
        self._by_source: dict[RuleSource, list[PermissionRule]] = {
            s: [] for s in self._ORDER
        }
        for r in rules:
            self._by_source[r.source].append(r)

    def all_rules(self) -> list[PermissionRule]:
        """Rules in evaluation order: highest-priority source first."""
        return [r for s in self._ORDER for r in self._by_source[s]]

    def add_session_rule(self, rule: PermissionRule) -> None:
        if rule.source is not RuleSource.SESSION:
            raise ValueError("only SESSION rules may be added at runtime")
        self._by_source[RuleSource.SESSION].append(rule)

    @classmethod
    def empty(cls) -> "SettingsHierarchy":
        return cls(())
