"""Permission modes that control how the engine treats undeclared cases.

Mirrors upstream Claude Code's mode set (`src/utils/permissions/PermissionMode.ts`)
minus the ant-only `auto` (classifier-based) mode, which adk-cc doesn't ship.
"""

from __future__ import annotations

from enum import Enum


class PermissionMode(str, Enum):
    DEFAULT = "default"
    """Normal mode. Rules evaluated in order; destructive tools without an
    explicit allow rule fall to `ask`."""

    PLAN = "plan"
    """Read-only planning. All write/destructive tools are blocked
    regardless of rules until the mode is changed."""

    ACCEPT_EDITS = "acceptEdits"
    """Auto-allow file edits without prompting. Other destructive tools
    still require confirmation."""

    BYPASS_PERMISSIONS = "bypassPermissions"
    """Skip rules entirely except deny rules. Use only with operator-trusted
    sessions."""

    DONT_ASK = "dontAsk"
    """Convert ask → deny. Strictest mode for unattended sessions."""
