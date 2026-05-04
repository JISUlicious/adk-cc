"""Tenant-scoped skill toolset (subclass of ADK's `BaseToolset`).

Resolves per-invocation: ADK calls `get_tools(readonly_context)` at the
start of each invocation, this class reads `tenant_id` from the
context's session state, scans `<skill_root>/<tenant_id>/` for skill
folders, loads them via ADK's `discover_skills` helper, and wraps them
in a `SkillToolset` whose `code_executor` is our
`SandboxBackedCodeExecutor`.

Hot reload comes for free: each invocation re-reads the directory, so
uploading or removing a skill (via admin routes or a direct disk
operation) takes effect on the next session without restarting the
agent process.

Why no registry? Skill folders ARE the registry — they live on disk
with a known shape (`<root>/<tenant>/<name>/`). An extra JSON index
would duplicate what `os.listdir` already provides. The credential
provider isn't used here because skills don't carry secrets.

The single-tenant `make_skill_toolset` factory in `tools/skills.py`
stays for non-service deployments wiring a single global skills dir
at boot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.code_executors.base_code_executor import BaseCodeExecutor
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.skill_toolset import SkillToolset

from .skills import discover_skills

_log = logging.getLogger(__name__)


class TenantSkillToolset(BaseToolset):
    """Per-tenant skill resolver. Wire into `LlmAgent.tools=[...]`."""

    def __init__(
        self,
        *,
        skill_root: str,
        code_executor: Optional[BaseCodeExecutor] = None,
        script_timeout: int = 300,
    ) -> None:
        super().__init__()
        self._skill_root = Path(skill_root)
        self._code_executor = code_executor
        self._script_timeout = script_timeout

    @staticmethod
    def _safe_tenant(tenant_id: str) -> str:
        safe = "".join(c for c in tenant_id if c.isalnum() or c in "-_")
        if safe != tenant_id or not safe:
            raise ValueError(f"unsafe tenant_id for filesystem: {tenant_id!r}")
        return safe

    async def get_tools(
        self, readonly_context: Optional[ReadonlyContext] = None
    ) -> list[BaseTool]:
        if readonly_context is None:
            return []
        try:
            state = readonly_context.session.state
            tenant = state.get("temp:tenant_context")
            tenant_id = (
                tenant.tenant_id
                if tenant is not None and hasattr(tenant, "tenant_id")
                else None
            )
        except Exception:
            tenant_id = None
        if not tenant_id:
            return []

        try:
            tenant_dir = self._skill_root / self._safe_tenant(tenant_id)
        except ValueError as e:
            _log.warning("TenantSkillToolset: %s", e)
            return []
        if not tenant_dir.is_dir():
            return []

        skills = discover_skills(tenant_dir)
        if not skills:
            return []
        inner = SkillToolset(
            skills=skills,
            code_executor=self._code_executor,
            script_timeout=self._script_timeout,
        )
        return await inner.get_tools_with_prefix(readonly_context)
