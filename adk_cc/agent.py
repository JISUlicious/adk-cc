"""Gather / act / verify agent loop on Google ADK 1.31.1.

Mirrors Claude Code's pattern from src/tools/AgentTool/built-in/:
  - One coordinator (the "main agent") owns user I/O.
  - Two specialists (Explore, verification) wired as `sub_agents`.
    Planning is NOT a sub-agent — it's a posture the coordinator takes
    when `permission_mode == "plan"` (see plugins/plan_mode.py).
    Delegation is an LLM-driven `transfer_to_agent` call — and because
    sub-agents share the parent's invocation context, all their tool
    calls and responses stream into the parent event log (visible in
    `adk web`), not buried inside an opaque tool result.

Forcing "coordinator owns user I/O" requires TWO mechanisms — neither
alone is enough:

  1. `disallow_transfer_to_parent=True` on each specialist. ADK's
     runner._find_agent_to_run walks events backward to pick whose turn
     it is and only accepts an agent for which
     _is_transferable_across_agent_tree() is True — which requires
     disallow_transfer_to_parent=False on the agent and all ancestors.
     Setting it to True on each specialist makes the runner skip them
     and fall back to the root (coordinator). This is the HARD guarantee
     that the next user message always lands on the coordinator.

  2. An after_agent_callback that yields a non-final-response event when
     the specialist finishes. base_llm_flow.run_async loops until
     last_event.is_final_response() returns True. A text-only message is
     final; a Content with a function_call Part is NOT (see Event.is_
     final_response in events/event.py). Yielding a synthetic function
     call as the specialist's last event keeps the parent's flow in its
     while-loop, which triggers another coordinator LLM step. The
     coordinator then sees the specialist's report in history and
     produces the user-facing reply.

  - `disallow_transfer_to_peers=True` blocks specialist→specialist hops.
  - Tool denylists stay structural: read-only specialists simply don't
    receive write tools.
  - The verifier's discipline stays prompt-enforced + parsed: it must end
    with `VERDICT: PASS|FAIL|PARTIAL`, which the coordinator's prompt
    tells it to look for.

Discovered by `adk web` / `adk run` via the module-level `root_agent`.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps.app import App
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from . import prompts
from .permissions import PermissionMode, SettingsHierarchy
from .plugins import (
    AuditPlugin,
    ContextGuardPlugin,
    PermissionPlugin,
    PlanModeReminderPlugin,
    TaskReminderPlugin,
    ToolCallValidatorPlugin,
)
from .tools import (
    AskUserQuestionTool,
    BashTool,
    EditFileTool,
    EnterPlanModeTool,
    ExitPlanModeTool,
    GlobFilesTool,
    GrepTool,
    ReadCurrentPlanTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WebFetchTool,
    WriteFileTool,
    WritePlanTool,
    make_skill_toolset,
)


def _force_coordinator_continuation(callback_context: Context) -> types.Content:
    """Force the parent flow to take another step after a specialist finishes.

    Returning a Content with a function_call Part makes the wrapping Event
    fail Event.is_final_response(), which keeps base_llm_flow.run_async in
    its while-True loop and triggers another coordinator LLM call. The
    coordinator then synthesizes the user-facing reply from the
    specialist's output in the conversation history.

    The synthetic call is for a no-op name, never executed; it's a control
    signal, not a real tool call.
    """
    return types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="_handback_to_coordinator",
                    args={},
                )
            )
        ],
    )

# Local model via LiteLLM, talking to an OpenAI-compatible server (mlx_lm /
# vLLM / llama.cpp / LM Studio). Defaults target localhost:18000 serving
# Qwen3.6-35B-A3B-UD-MLX-4bit. ADK_CC_API_KEY is loaded by `adk web` /
# `adk run` from .env in the agent directory. Override any of:
#   ADK_CC_MODEL=openai/<model-id>
#   ADK_CC_API_BASE=http://host:port/v1
#   ADK_CC_API_KEY=<token>
MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)


# ---------- shared tool instances ----------
# Tools are stateless; one instance per tool, reused across agents.
_read_file = ReadFileTool()
_glob_files = GlobFilesTool()
_grep = GrepTool()
_write_file = WriteFileTool()
_edit_file = EditFileTool()
_run_bash = BashTool()
_web_fetch = WebFetchTool()
_ask_user = AskUserQuestionTool()
_task_create = TaskCreateTool()
_task_get = TaskGetTool()
_task_list = TaskListTool()
_task_update = TaskUpdateTool()
_exit_plan_mode = ExitPlanModeTool()
_enter_plan_mode = EnterPlanModeTool()
_write_plan = WritePlanTool()
_read_current_plan = ReadCurrentPlanTool()
_skills = make_skill_toolset()  # None unless ADK_CC_SKILLS_DIR / skills/ has content


def _make_tenant_mcp_toolset():
    """Construct the per-tenant MCP toolset if env config is present.

    Returns None when this is a single-tenant deployment without per-tenant
    MCP wiring (the common dev path); the coordinator's tools list then
    skips this entry. For service deployments, set:

        ADK_CC_TENANT_REGISTRY_DIR=/var/lib/adk-cc/tenants
        ADK_CC_CREDENTIAL_PROVIDER=encrypted_file
        ADK_CC_CREDENTIAL_STORE_DIR=/var/lib/adk-cc/credentials
        ADK_CC_CREDENTIAL_KEY=<fernet-key>
    """
    registry_dir = os.environ.get("ADK_CC_TENANT_REGISTRY_DIR")
    if not registry_dir:
        return None

    from .credentials import (
        EncryptedFileCredentialProvider,
        InMemoryCredentialProvider,
    )
    from .service.registry import JsonFileTenantResourceRegistry
    from .tools.mcp_tenant import McpServerConfig, TenantMcpToolset

    provider_kind = os.environ.get("ADK_CC_CREDENTIAL_PROVIDER", "memory").lower()
    if provider_kind == "encrypted_file":
        store_dir = os.environ.get("ADK_CC_CREDENTIAL_STORE_DIR")
        if not store_dir:
            raise RuntimeError(
                "ADK_CC_CREDENTIAL_PROVIDER=encrypted_file requires "
                "ADK_CC_CREDENTIAL_STORE_DIR to be set"
            )
        creds = EncryptedFileCredentialProvider(root=store_dir)
    elif provider_kind == "memory":
        creds = InMemoryCredentialProvider()
    else:
        raise RuntimeError(
            f"unknown ADK_CC_CREDENTIAL_PROVIDER={provider_kind!r}; "
            "valid: memory, encrypted_file"
        )

    registry = JsonFileTenantResourceRegistry[McpServerConfig](
        root=registry_dir,
        kind="mcp",
        model=McpServerConfig,
        id_attr="server_name",
    )
    return TenantMcpToolset(registry=registry, credentials=creds)


_tenant_mcp = _make_tenant_mcp_toolset()


def _make_tenant_skill_toolset():
    """Construct the per-tenant skill toolset if env config is present.

    Returns None when this is a single-tenant deployment using the
    static `make_skill_toolset` factory above. For service deployments
    set:

        ADK_CC_TENANT_SKILLS_DIR=/var/lib/adk-cc/skills

    Skills land at `<root>/<tenant_id>/<skill_name>/`. Skill scripts run
    inside the active session's sandbox via `SandboxBackedCodeExecutor`.
    """
    skill_root = os.environ.get("ADK_CC_TENANT_SKILLS_DIR")
    if not skill_root:
        return None

    from .sandbox.code_executor import SandboxBackedCodeExecutor
    from .tools.skills_tenant import TenantSkillToolset

    return TenantSkillToolset(
        skill_root=skill_root,
        code_executor=SandboxBackedCodeExecutor(),
    )


_tenant_skills = _make_tenant_skill_toolset()


# ---------- specialist agents (read-only) ----------

explore_agent = LlmAgent(
    name="Explore",
    model=MODEL,
    description=(
        "Fast read-only codebase explorer. Use for broad searches across files "
        "or when a question will take more than ~3 directed queries to answer. "
        "Returns a written report; does not modify files."
    ),
    instruction=prompts.EXPLORE_INSTRUCTION,
    tools=[_read_file, _glob_files, _grep, _web_fetch],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)

verify_agent = LlmAgent(
    name="verification",
    model=MODEL,
    description=(
        "Adversarial verifier. Runs builds, tests, linters, and adversarial "
        "probes against changes. Cannot modify the project (writes to /tmp "
        "only via run_bash). Always ends with a literal "
        "'VERDICT: PASS|FAIL|PARTIAL' line. Invoke after non-trivial "
        "implementation (3+ file edits, backend/API, or infra changes)."
    ),
    instruction=prompts.VERIFY_INSTRUCTION,
    tools=[_read_file, _glob_files, _grep, _run_bash, _web_fetch, _read_current_plan],
    disallow_transfer_to_parent=True,
    disallow_transfer_to_peers=True,
    after_agent_callback=_force_coordinator_continuation,
)


# ---------- coordinator (the "main agent") ----------

_coordinator_tools: list = [
    _read_file,
    _glob_files,
    _grep,
    _write_file,
    _edit_file,
    _run_bash,
    _web_fetch,
    _ask_user,
    _task_create,
    _task_get,
    _task_list,
    _task_update,
    _exit_plan_mode,
    _enter_plan_mode,
    _write_plan,
    _read_current_plan,
]
if _skills is not None:
    _coordinator_tools.append(_skills)
if _tenant_mcp is not None:
    _coordinator_tools.append(_tenant_mcp)
if _tenant_skills is not None:
    _coordinator_tools.append(_tenant_skills)

root_agent = LlmAgent(
    name="coordinator",
    model=MODEL,
    description="Coordinator agent: handles user requests with a gather → act → verify loop.",
    instruction=prompts.COORDINATOR_INSTRUCTION,
    tools=_coordinator_tools,
    sub_agents=[explore_agent, verify_agent],
)


# ---------- ADK events compaction (primary context-length defense) ----------
# When configured via env, ADK runs token-threshold or sliding-window
# compaction post-invocation via LlmEventSummarizer. The thin
# ContextGuardPlugin below adds pre-flight WARN logging and fail-soft
# REJECT for the case where a single turn jumps past the window before
# ADK can react. See plan-mode plan + docs/05-production-deployment.md.


def _make_lazy_summarizer_class():
    """Build the lazy summarizer class with deferred BaseEventsSummarizer import.

    Returns a `BaseEventsSummarizer` + `BaseModel` subclass that stores config
    strings as pydantic fields only — never a live `LiteLlm` — so the
    surrounding `EventsCompactionConfig` stays JSON-serializable. ADK's
    flow / OTel / FastAPI paths walk the InvocationContext (which carries
    the config); a `LiteLlm` sitting on `summarizer._llm` leaks
    `LiteLLMClient` into pydantic's `dump_json` step and trips with
    `PydanticSerializationError: Unable to serialize unknown type`.

    The actual `LlmEventSummarizer` + `LiteLlm` are constructed once per
    compaction call. One extra ~ms object construction; eliminates the
    serialization hazard.
    """
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
    from pydantic import BaseModel, Field
    from typing import Optional

    class _LazyAdkCcSummarizer(BaseModel, BaseEventsSummarizer):
        # Plain string fields so pydantic dump_json works.
        model_id: str
        api_base: Optional[str] = None
        # Exclude api_key from dumps so it doesn't leak into logs / traces
        # if anything serializes the surrounding config.
        api_key: Optional[str] = Field(default=None, exclude=True, repr=False)
        prompt_template: Optional[str] = None

        async def maybe_summarize_events(self, *, events):
            from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

            llm = LiteLlm(
                model=self.model_id,
                api_base=self.api_base,
                api_key=self.api_key,
            )
            inner = LlmEventSummarizer(llm=llm, prompt_template=self.prompt_template)
            return await inner.maybe_summarize_events(events=events)

    return _LazyAdkCcSummarizer


def _make_compaction_summarizer():
    """Build an env-driven summarizer when a dedicated compaction model is
    configured. Returns None to let ADK auto-default to the agent's model
    (its lazy `_ensure_compaction_summarizer` instantiates LlmEventSummarizer
    just-in-time, so the default path doesn't trip serialization either)."""
    model_id = os.environ.get("ADK_CC_COMPACTION_MODEL")
    if not model_id:
        return None
    api_base = os.environ.get(
        "ADK_CC_COMPACTION_API_BASE", os.environ.get("ADK_CC_API_BASE")
    )
    api_key = os.environ.get(
        "ADK_CC_COMPACTION_API_KEY", os.environ.get("ADK_CC_API_KEY", "")
    )
    cls = _make_lazy_summarizer_class()
    return cls(model_id=model_id, api_base=api_base, api_key=api_key)


def _make_compaction_config():
    """Construct EventsCompactionConfig from env. Returns None if disabled."""
    threshold = os.environ.get("ADK_CC_COMPACTION_TOKEN_THRESHOLD")
    retention = os.environ.get("ADK_CC_COMPACTION_EVENT_RETENTION")
    interval = os.environ.get("ADK_CC_COMPACTION_INTERVAL")
    overlap = os.environ.get("ADK_CC_COMPACTION_OVERLAP")
    if not (threshold or interval):
        return None  # compaction disabled
    if bool(threshold) != bool(retention):
        raise RuntimeError(
            "ADK_CC_COMPACTION_TOKEN_THRESHOLD and ADK_CC_COMPACTION_EVENT_RETENTION "
            "must be set together (ADK's EventsCompactionConfig validator requires both or neither)."
        )
    try:
        from google.adk.apps.app import EventsCompactionConfig
    except ImportError:
        # ADK's compaction is @experimental; tolerate import breakage.
        import logging

        logging.getLogger(__name__).warning(
            "ADK EventsCompactionConfig unavailable; skipping compaction wiring."
        )
        return None
    return EventsCompactionConfig(
        token_threshold=int(threshold) if threshold else None,
        event_retention_size=int(retention) if retention else None,
        # Required fields even when only token-threshold is wanted.
        compaction_interval=int(interval) if interval else 10,
        overlap_size=int(overlap) if overlap else 2,
        summarizer=_make_compaction_summarizer(),
    )


_compaction_config = _make_compaction_config()


# ---------- App with permission plugin ----------
# `adk web` / `adk run` look for `app` first, then `root_agent`. Exposing
# both keeps direct-test imports of `root_agent` working while letting the
# CLI wire the plugin chain automatically.
#
# Default mode is `bypassPermissions` to preserve the dev experience: the
# plugin is always loaded (so Stage D/G can layer audit/quotas on top),
# but it only enforces deny rules. Flip to `default`/`plan`/`acceptEdits`/
# `dontAsk` via env to exercise the engine.
PERMISSION_MODE = PermissionMode(
    os.environ.get("ADK_CC_PERMISSION_MODE", PermissionMode.BYPASS_PERMISSIONS.value)
)
SETTINGS = SettingsHierarchy.empty()  # rules added by operators / Stage G loader

_app_kwargs = dict(
    name="adk_cc",
    root_agent=root_agent,
    # Order matters. Audit goes first so before_tool_callback records every
    # attempt — including ones the permission plugin denies. Permission's
    # short-circuit only stops the *chain*, but audit's row is already
    # written by then.
    plugins=[
        AuditPlugin(),
        PermissionPlugin(SETTINGS, default_mode=PERMISSION_MODE),
        # Reminders run on before_model_callback, lifecycle independent of
        # the before_tool chain — order relative to others doesn't matter.
        PlanModeReminderPlugin(),
        TaskReminderPlugin(),
        # Catches "tool not found" errors from ADK's tool dispatch and
        # turns them into corrective tool responses so the model can
        # self-correct on the next iteration instead of aborting the run.
        ToolCallValidatorPlugin(),
        # Pre-flight context-length guardrail: WARN at 75% of MAX,
        # REJECT at 95%. ADK's EventsCompactionConfig (set above) is
        # the primary defense; this is the fail-soft safety net.
        ContextGuardPlugin(),
    ],
)
if _compaction_config is not None:
    _app_kwargs["events_compaction_config"] = _compaction_config

app = App(**_app_kwargs)
