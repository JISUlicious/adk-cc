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

from .models.selectable import resolve_max_output_tokens

from . import deployment
from . import prompts
from .logging_setup import configure_logging
from .permissions import PermissionMode, SettingsHierarchy

# Apply env-driven logging config (ADK_CC_LOG_LEVEL, ADK_CC_LOG_FORMAT)
# before any submodule logger fires. Idempotent — safe across reimports.
configure_logging()
from .plugins import (
    AskPausePlugin,
    AskUserQuestionUiHintPlugin,
    AuditPlugin,
    AuthzPlugin,
    ModelIOTracePlugin,
    ProjectContextPlugin,
    ConfirmationFormUiPlugin,
    ContextGuardPlugin,
    McpExportArtifactPlugin,
    MicrocompactPlugin,
    PermissionPlugin,
    PlanModeReminderPlugin,
    QuotaPlugin,
    TaskReminderPlugin,
    ToolCallValidatorPlugin,
    WorkspaceHintPlugin,
)
from .plugins.secret_redaction import SecretRedactionPlugin
from .plugins.truncated_tool_call import TruncatedToolCallPlugin
from .credentials import credential_provider_from_env
from .service.tenancy import TenancyPlugin
from .tools import (
    AskUserQuestionTool,
    BashTool,
    EditFileTool,
    EnterPlanModeTool,
    ExitPlanModeTool,
    GlobFilesTool,
    GrepTool,
    LoadArtifactToSandboxTool,
    ReadCurrentPlanTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WebFetchTool,
    WriteFileTool,
    SaveAsArtifactTool,
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
_BOOT_MODEL_ID = os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit")
_BOOT_API_BASE = os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1")


def _build_boot_litellm(max_tokens):
    """Build the boot/default LiteLlm at a given output cap (falsy → uncapped).

    Used both for the base delegate and — handed to SelectableLlm as the
    ``default_delegate_factory`` — for the higher-cap rebuild on escalation, so
    the escalated copy is built the same way instead of scraping LiteLlm internals.
    """
    return LiteLlm(
        model=_BOOT_MODEL_ID,
        api_base=_BOOT_API_BASE,
        # `.get` (not `[...]`) so a fresh desktop install with no key yet still
        # BOOTS — the UI loads and the user can fill ADK_CC_API_KEY in their
        # settings.env; model calls fail with a clear auth error until they do.
        api_key=os.environ.get("ADK_CC_API_KEY", ""),
        # Cap output tokens when configured (ADK_CC_MAX_OUTPUT_TOKENS) — prevents
        # the model stopping mid tool-call on endpoints with a low output limit.
        **({"max_tokens": max_tokens} if max_tokens else {}),
    )


if not os.environ.get("ADK_CC_API_KEY"):
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "ADK_CC_API_KEY is not set — the server starts, but model calls will fail "
        "until you set it (desktop app: ~/.adk-cc-desktop/settings.env)."
    )

_boot_litellm = _build_boot_litellm(resolve_max_output_tokens())


def _make_model():
    """The agent's model — ALWAYS a SelectableLlm wrapping the boot LiteLlm.

    The SelectableLlm resolves the active endpoint LAZILY from
    ADK_CC_MODEL_REGISTRY_FILE on each request. This object is built at
    package import (eager), which is BEFORE make_app's _prepare_admin_env
    sets that env var — so resolution must be lazy, not at construction.

    When no registry file is configured / no active endpoint exists (admin
    panel off — the default), SelectableLlm falls through to the boot
    LiteLlm delegate, so behavior is IDENTICAL to the pre-panel single
    model. When the panel is on, _prepare_admin_env sets the env var and
    seeds the boot model as endpoint #1, and an admin activate switches the
    live model with no restart.
    """
    from .models import SelectableLlm

    return SelectableLlm(
        registry_path_env="ADK_CC_MODEL_REGISTRY_FILE",
        default_delegate=_boot_litellm,
        default_delegate_factory=_build_boot_litellm,
        default_model_id=_BOOT_MODEL_ID,
    )


MODEL = _make_model()

# ---------- permission mode (env-driven) ----------
# Hoisted above tool instantiation so the plan-mode tools can use it as
# their `default_mode` fallback (mirroring the plugin-side fix in PR #4).
# Default `bypassPermissions` preserves the dev experience: permissions
# plugin is always loaded (so audit/quota/etc can layer on top) but only
# enforces deny rules. Flip via env to exercise plan/default/acceptEdits/dontAsk.
PERMISSION_MODE = PermissionMode(
    os.environ.get("ADK_CC_PERMISSION_MODE", PermissionMode.BYPASS_PERMISSIONS.value)
)
# Permission rules: honor ADK_CC_PERMISSIONS_YAML if set. Falls back to
# empty hierarchy so operators can still drive everything through
# PERMISSION_MODE alone. Loading here (rather than in the FastAPI
# factory) means `adk web .` and the FastAPI deployment both pick up
# the YAML uniformly.
_permissions_yaml = os.environ.get("ADK_CC_PERMISSIONS_YAML")
if _permissions_yaml:
    from .config import load_settings_from_yaml as _load_settings_from_yaml

    SETTINGS = _load_settings_from_yaml(_permissions_yaml)
else:
    SETTINGS = SettingsHierarchy.empty()


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
# Plan-mode tools take the env default so their internal "previous mode"
# check sees the right value on a fresh session booted with
# ADK_CC_PERMISSION_MODE=plan. Without this fallback, exit_plan_mode reads
# state["permission_mode"]=None, the `previous != "plan"` guard trips, and
# state never flips to "default" — the session stays stuck in plan mode.
_exit_plan_mode = ExitPlanModeTool(default_mode=PERMISSION_MODE.value)
_enter_plan_mode = EnterPlanModeTool(default_mode=PERMISSION_MODE.value)
_write_plan = WritePlanTool()
_save_as_artifact = SaveAsArtifactTool()
_load_artifact_to_sandbox = LoadArtifactToSandboxTool()
_read_current_plan = ReadCurrentPlanTool()


def _artifacts_supported() -> bool:
    """Whether the artifact tools (save_as_artifact / load_artifact_to_sandbox)
    should be exposed.

    They move bytes between ADK's artifact store and the sandbox filesystem,
    which only makes sense with a REAL sandbox. Under the `noop` backend
    (dev / host-exec, the default) there's no sandbox to load into — the
    base backend even decodes bytes as UTF-8, so loading a binary artifact
    would crash. So we don't list these tools when the backend is noop.

    Decided here from ADK_CC_SANDBOX_BACKEND (known at construction, default
    "noop"); a per-session backend override is caught by the tools' own
    runtime guard."""
    return deployment.sandbox_backend_name() != "noop"
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


def _make_static_mcp_toolset():
    """Wire a single static MCP server from env (single-tenant / dev).

    Returns None unless `ADK_CC_MCP_SERVER` is set. Env:
      - ADK_CC_MCP_SERVER     : the stdio command line to launch the server
                                (e.g. "python tests/fixtures/csv_mcp_server.py").
      - ADK_CC_MCP_SERVER_NAME: logical name / tool prefix (default "mcp").
      - ADK_CC_MCP_TRANSPORT  : stdio | sse | http (default stdio). For
                                sse/http, ADK_CC_MCP_SERVER is the URL.
      - ADK_CC_MCP_SAVE_RESOURCES_AS_ARTIFACTS : 1 to add the
                                save_resource_as_artifact tool (Pattern A).
      - ADK_CC_MCP_USE_RESOURCES : 1 to add load_mcp_resource + inject the
                                resource catalog into context.
    """
    server = os.environ.get("ADK_CC_MCP_SERVER")
    if not server:
        return None
    from .tools.mcp import connection_params_for, make_mcp_toolset

    name = os.environ.get("ADK_CC_MCP_SERVER_NAME", "mcp")
    transport = os.environ.get("ADK_CC_MCP_TRANSPORT", "stdio")
    params = connection_params_for(transport, server)

    return make_mcp_toolset(
        server_name=name,
        connection_params=params,
        save_resources_as_artifacts=(
            os.environ.get("ADK_CC_MCP_SAVE_RESOURCES_AS_ARTIFACTS") == "1"
        ),
        use_mcp_resources=(os.environ.get("ADK_CC_MCP_USE_RESOURCES") == "1"),
    )


_static_mcp = _make_static_mcp_toolset()


def _make_static_mcp_toolsets():
    """All boot-time MCP toolsets: the single `ADK_CC_MCP_SERVER` server (if
    set) PLUS every server listed in `ADK_CC_MCP_SERVERS_FILE` (a JSON array
    of McpServerConfig). The single env server stays fully back-compatible;
    the file is additive. A file entry whose server_name collides with the
    single env server is dropped (the loader warns), since the
    `mcp__<name>__` tool prefixes would clash."""
    from .tools.mcp import load_static_mcp_servers

    toolsets = []
    exclude = set()
    if _static_mcp is not None:
        toolsets.append(_static_mcp)
        # The single env server's logical name (default "mcp") — exclude it
        # from the file load so a same-named file entry can't double-wire it.
        exclude.add(os.environ.get("ADK_CC_MCP_SERVER_NAME", "mcp"))
    toolsets.extend(load_static_mcp_servers(exclude_names=frozenset(exclude)))
    return toolsets


_static_mcp_toolsets = _make_static_mcp_toolsets()


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
# Artifact tools only make sense with a real sandbox — omit them under the
# noop backend (default dev/host-exec) so the model never calls a tool that
# can't meaningfully run. See _artifacts_supported(); the tools also guard
# at call time against a per-session noop override.
if _artifacts_supported():
    _coordinator_tools.append(_save_as_artifact)
    _coordinator_tools.append(_load_artifact_to_sandbox)
if _skills is not None:
    _coordinator_tools.append(_skills)
# Static MCP servers: the single ADK_CC_MCP_SERVER (back-compat) plus any
# from ADK_CC_MCP_SERVERS_FILE — see _make_static_mcp_toolsets().
_coordinator_tools.extend(_static_mcp_toolsets)
if _tenant_mcp is not None:
    _coordinator_tools.append(_tenant_mcp)
if _tenant_skills is not None:
    _coordinator_tools.append(_tenant_skills)

# Knowledge-wiki memory tools (opt-in, ADK_CC_WIKI=1). User-scope writes
# only: wiki_add captures to the caller's PRIVATE inbox; the offline
# librarian (scripts/wiki_librarian.py) merges vetted notes into the
# shared domain wiki. wiki_search / wiki_read overlay the caller's private
# notes on the shared wiki. Inert unless the flag is set, so the
# dev/default tool surface is unchanged. Explore (read-only) gets the
# recall tools too; only the coordinator can capture.
if os.environ.get("ADK_CC_WIKI") == "1":
    from .tools import WikiAddTool, WikiReadTool, WikiSearchTool

    _wiki_search, _wiki_read = WikiSearchTool(), WikiReadTool()
    _coordinator_tools.extend([_wiki_search, _wiki_read, WikiAddTool()])
    explore_agent.tools.extend([_wiki_search, _wiki_read])

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


# Structured compaction summary prompt (adapted from Claude Code's
# src/services/compact/prompt.ts — analysis/compaction-prompt-plan.md). ADK's
# LlmEventSummarizer interpolates via `template.format(conversation_history=...)`,
# so this MUST contain exactly one `{conversation_history}` and NO other literal
# braces (str.format would choke). The `<analysis>` scratchpad improves quality
# and is stripped post-hoc by `_strip_analysis` before the summary enters context.
_ADKCC_COMPACTION_PROMPT = (
    "You are summarizing a conversation between a user and an AI agent so the "
    "agent can continue working after older messages are dropped from its "
    "context. Capture technical detail, decisions, and the user's intent "
    "faithfully.\n\n"
    "First, think in an <analysis> block: go through the conversation "
    "chronologically and note the user's explicit requests, your approach, key "
    "technical decisions, specific file names / code / commands, errors and "
    "their fixes, and especially any user feedback or corrections. Then write "
    "the summary.\n\n"
    "Wrap the final summary in a <summary> block with these sections:\n"
    "1. Primary Request and Intent: all explicit user requests and intent.\n"
    "2. Key Technical Concepts: technologies, frameworks, patterns discussed.\n"
    "3. Files and Code Sections: files/commands examined or changed, with the "
    "important snippets and why they matter.\n"
    "4. Errors and Fixes: errors hit and how they were resolved, plus user "
    "feedback received.\n"
    "5. Problem Solving: what was solved and any ongoing troubleshooting.\n"
    "6. User Messages: the non-tool user messages, to preserve intent and "
    "feedback.\n"
    "7. Pending Tasks: work explicitly still requested.\n"
    "8. Current Work: precisely what was being done just before this summary.\n"
    "9. Next Step: the immediate next step, if it directly continues the most "
    "recent work; otherwise omit.\n\n"
    "Output ONLY the <analysis> block followed by the <summary> block.\n\n"
    "Conversation:\n{conversation_history}"
)

_COMPACTION_PLACEHOLDER = "{conversation_history}"


def _resolve_compaction_prompt() -> str:
    """Pick the compaction summary template: inline env override, then file
    override, then the structured default. Guarantees the
    `{conversation_history}` placeholder is present (appends it if a custom
    template omits it, so ADK's .format() still receives the history)."""
    template = os.environ.get("ADK_CC_COMPACTION_PROMPT")
    if not template:
        path = os.environ.get("ADK_CC_COMPACTION_PROMPT_FILE")
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    template = fh.read()
            except OSError:
                template = None
    if not template:
        template = _ADKCC_COMPACTION_PROMPT
    if _COMPACTION_PLACEHOLDER not in template:
        template = template.rstrip() + "\n\n" + _COMPACTION_PLACEHOLDER
    return template


def _strip_analysis(event):
    """Strip the <analysis> scratchpad and unwrap <summary> from a compaction
    Event's text, in place — mirrors CC's formatCompactSummary. The scratchpad
    improves the summary but must not enter the context compaction is shrinking.
    Tolerates the various shapes (Content with parts, mocks); degrades to the raw
    text (minus analysis) when the model didn't follow the structure. Never
    empties the summary."""
    import re as _re

    def _clean(text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        cleaned = _re.sub(r"<analysis>.*?</analysis>", "", text,
                          flags=_re.DOTALL | _re.IGNORECASE).strip()
        m = _re.search(r"<summary>(.*?)</summary>", cleaned,
                       flags=_re.DOTALL | _re.IGNORECASE)
        if m:
            cleaned = m.group(1).strip()
        else:
            # no <summary> tag (weak model) — just drop any stray tags
            cleaned = _re.sub(r"</?summary>", "", cleaned,
                              flags=_re.IGNORECASE).strip()
        return cleaned or text.strip()  # never return empty

    try:
        content = event.actions.compaction.compacted_content
    except AttributeError:
        return event
    parts = getattr(content, "parts", None) or []
    for part in parts:
        if getattr(part, "text", None):
            part.text = _clean(part.text)
    return event


def _seed_memory_into_summary(event, events):
    """P3 bridge: prepend the user's recalled durable memory to the compaction
    summary, so durable facts are carried INSIDE the boundary (not only
    re-injected each turn by MemoryPlugin recall). Opt-in
    (ADK_CC_COMPACTION_SEED_MEMORY=1); best-effort — any failure / missing
    principal / empty recall leaves the summary untouched."""
    if os.environ.get("ADK_CC_COMPACTION_SEED_MEMORY") != "1":
        return event
    try:
        from .memory import MemoryStore, get_principal, recall_context

        principal = get_principal()
        if not principal:
            return event
        tenant_id, user_id = principal
        # Rank recall by the most recent user text in the compacted events.
        query = ""
        for e in reversed(list(events or [])):
            content = getattr(e, "content", None)
            if getattr(content, "role", None) == "user":
                query = " ".join(
                    p.text for p in (getattr(content, "parts", None) or [])
                    if getattr(p, "text", None)
                )
                if query:
                    break
        try:
            budget = max(0, int(os.environ.get("ADK_CC_COMPACTION_SEED_BUDGET", "")))
        except ValueError:
            budget = 300
        block = recall_context(
            MemoryStore.for_tenant(tenant_id), user_id, query or "user project context",
            budget_tokens=budget or 300,
        )
        if not block:
            return event
        preamble = "Durable facts about this user (from memory):\n" + block + "\n\n"
        content = event.actions.compaction.compacted_content
        parts = getattr(content, "parts", None) or []
        if parts and getattr(parts[0], "text", None) is not None:
            parts[0].text = preamble + parts[0].text
        else:
            from google.genai import types as _types
            content.parts = [_types.Part(text=preamble)] + list(parts)
    except Exception:  # noqa: BLE001 — seeding must never break a compaction
        return event
    return event


_COMPACTION_FRAME_DEFAULT = (
    "[The following condenses earlier messages in this session to save context. "
    "Continue the conversation directly — do not acknowledge or recap this "
    "summary.]"
)


def _frame_summary(event):
    """P5: prepend a continuation instruction to the compaction summary so the
    model resumes silently instead of narrating the compaction (CC's
    getCompactUserSummaryMessage intent, adapted for ADK's in-window summary).
    Default on; ADK_CC_COMPACTION_FRAME=0 disables, or set it to a custom line."""
    raw = os.environ.get("ADK_CC_COMPACTION_FRAME")
    if raw == "0":
        return event
    frame = raw if raw else _COMPACTION_FRAME_DEFAULT
    try:
        content = event.actions.compaction.compacted_content
        parts = getattr(content, "parts", None) or []
        if parts and getattr(parts[0], "text", None) is not None:
            parts[0].text = frame + "\n\n" + parts[0].text
        else:
            from google.genai import types as _types
            content.parts = [_types.Part(text=frame)] + list(parts)
    except Exception:  # noqa: BLE001 — framing must never break a compaction
        return event
    return event


class _CompactionBreaker:
    """Process-global circuit breaker for the compaction summarizer (P6).

    After N consecutive failures (timeout / exception / empty), open the breaker
    for a cooldown so we stop hammering a failing — often rate-limited — model
    endpoint with summarizer calls. Closes on the first success. Mirrors CC's
    MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES. Thread-safe; `now` injectable for tests.

    Config: ADK_CC_COMPACTION_BREAKER_THRESHOLD (default 3; 0 disables),
            ADK_CC_COMPACTION_BREAKER_COOLDOWN_S (default 60).
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._fails = 0
        self._open_until = 0.0

    @staticmethod
    def _threshold() -> int:
        try:
            return max(0, int(os.environ.get("ADK_CC_COMPACTION_BREAKER_THRESHOLD", "")))
        except ValueError:
            return 3

    @staticmethod
    def _cooldown() -> float:
        try:
            return max(0.0, float(os.environ.get("ADK_CC_COMPACTION_BREAKER_COOLDOWN_S", "")))
        except ValueError:
            return 60.0

    def should_skip(self, now: float) -> bool:
        if self._threshold() <= 0:
            return False
        with self._lock:
            return now < self._open_until

    def record_failure(self, now: float) -> None:
        thr = self._threshold()
        with self._lock:
            self._fails += 1
            if thr > 0 and self._fails >= thr:
                self._open_until = now + self._cooldown()

    def record_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._open_until = 0.0


_COMPACTION_BREAKER = _CompactionBreaker()


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

    Audit + DEBUG instrumentation wraps the inner call:
      - `compaction_triggered` before the summarizer runs
      - `compaction_success`   on a non-None return
      - `compaction_failure`   on a None return, timeout, or exception
    ADK's `LlmEventSummarizer` returns `None` on its own internal
    failure modes (empty input, malformed model response). The wrapper
    distinguishes the three failure modes via the `reason` field:
    `empty_summary` / `timeout` / `exception`.

    Timeout + graceful degradation:
      - `asyncio.wait_for(coro, timeout=timeout_seconds)` wraps the
        inner call when `timeout_seconds > 0`.
      - On `asyncio.TimeoutError`, the wrapper logs WARN, emits
        `compaction_failure` with `reason="timeout"`, and returns
        `None`. ADK treats `None` the same as "no summary produced"
        — the turn proceeds with uncompacted history (model sees a
        larger context once, but the agent does NOT hang waiting for
        a stuck summarizer).
      - On any other exception, the wrapper logs WARN, emits
        `compaction_failure` with `reason="exception"`, and ALSO
        returns `None` rather than re-raising. Same "agent must
        survive the failure" reasoning — a broken summarizer can
        only cost an occasional uncompacted turn, never a stuck
        session.
      - `timeout_seconds=0` disables the timeout entirely (preserves
        the pre-PR-B behavior of indefinite wait). Exceptions still
        degrade to None.

    The wrapper is ALWAYS installed when compaction is enabled (any
    of the COMPACTION env vars set) — `_make_compaction_summarizer`
    falls back to the main-agent model env vars when
    `ADK_CC_COMPACTION_MODEL` is unset. Same effective behavior as
    ADK's default summarizer (which would auto-instantiate
    `LlmEventSummarizer(llm=agent.canonical_model)`), plus our
    observability hooks. So operators get audit + DEBUG visibility
    just by enabling compaction; no extra "set this model env var"
    gotcha.
    """
    import asyncio
    import logging
    import time
    from google.adk.apps.base_events_summarizer import BaseEventsSummarizer
    from pydantic import BaseModel, Field
    from typing import Optional

    from .plugins.audit import emit_compaction_event

    _compaction_log = logging.getLogger(__name__ + ".compaction")

    class _LazyAdkCcSummarizer(BaseModel, BaseEventsSummarizer):
        # Plain string fields so pydantic dump_json works.
        model_id: str
        api_base: Optional[str] = None
        # Exclude api_key from dumps so it doesn't leak into logs / traces
        # if anything serializes the surrounding config.
        api_key: Optional[str] = Field(default=None, exclude=True, repr=False)
        prompt_template: Optional[str] = None
        # Seconds; 0 disables the timeout entirely (pre-PR-B behavior).
        # `_make_compaction_summarizer` reads ADK_CC_COMPACTION_TIMEOUT_S
        # (default 30) once at construction; this field is the resolved
        # value.
        timeout_seconds: float = 30.0

        async def maybe_summarize_events(self, *, events):
            from google.adk.apps.llm_event_summarizer import LlmEventSummarizer

            event_count = len(events) if events else 0
            last_event_ts = None
            if events:
                # The Event dataclass has `timestamp`; tolerate missing
                # field (defensive — tests pass mock events).
                last_event_ts = getattr(events[-1], "timestamp", None)

            if _compaction_log.isEnabledFor(logging.DEBUG):
                _compaction_log.debug(
                    "compaction_triggered model=%s events=%s timeout=%s",
                    self.model_id,
                    event_count,
                    self.timeout_seconds,
                    extra={
                        "model_id": self.model_id,
                        "event_count": event_count,
                        "timeout_seconds": self.timeout_seconds,
                    },
                )
            emit_compaction_event(
                "compaction_triggered",
                model_id=self.model_id,
                event_count=event_count,
                last_event_ts=last_event_ts,
                timeout_seconds=self.timeout_seconds,
            )

            started = time.monotonic()
            # Circuit breaker (P6): if recent summarizer calls keep failing, skip
            # the model call during the cooldown — the turn proceeds uncompacted
            # rather than hammering a failing/rate-limited endpoint.
            if _COMPACTION_BREAKER.should_skip(started):
                _compaction_log.warning(
                    "compaction_skipped model=%s reason=breaker_open", self.model_id)
                emit_compaction_event(
                    "compaction_failure", model_id=self.model_id,
                    reason="breaker_open", event_count=event_count)
                return None
            try:
                llm = LiteLlm(
                    model=self.model_id,
                    api_base=self.api_base,
                    api_key=self.api_key,
                )
                inner = LlmEventSummarizer(
                    llm=llm, prompt_template=self.prompt_template
                )
                # Apply timeout when configured (>0). At 0 we preserve
                # the original "wait forever" semantics so an operator
                # who explicitly opts out gets the pre-PR-B behavior.
                if self.timeout_seconds > 0:
                    result = await asyncio.wait_for(
                        inner.maybe_summarize_events(events=events),
                        timeout=self.timeout_seconds,
                    )
                else:
                    result = await inner.maybe_summarize_events(events=events)
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                _compaction_log.warning(
                    "compaction_failure model=%s reason=timeout "
                    "timeout_seconds=%s elapsed_ms=%s",
                    self.model_id,
                    self.timeout_seconds,
                    elapsed_ms,
                    extra={
                        "model_id": self.model_id,
                        "reason": "timeout",
                        "timeout_seconds": self.timeout_seconds,
                        "elapsed_ms": elapsed_ms,
                    },
                )
                emit_compaction_event(
                    "compaction_failure",
                    model_id=self.model_id,
                    reason="timeout",
                    timeout_seconds=self.timeout_seconds,
                    elapsed_ms=elapsed_ms,
                )
                # Graceful degrade: return None so ADK skips this
                # compaction. The turn proceeds with uncompacted
                # history rather than hanging on a stuck summarizer.
                _COMPACTION_BREAKER.record_failure(time.monotonic())
                return None
            except Exception as e:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                _compaction_log.warning(
                    "compaction_failure model=%s reason=exception "
                    "error_type=%s error=%s elapsed_ms=%s",
                    self.model_id,
                    type(e).__name__,
                    e,
                    elapsed_ms,
                    extra={
                        "model_id": self.model_id,
                        "reason": "exception",
                        "error_type": type(e).__name__,
                        "elapsed_ms": elapsed_ms,
                    },
                )
                emit_compaction_event(
                    "compaction_failure",
                    model_id=self.model_id,
                    reason="exception",
                    error_type=type(e).__name__,
                    error_message=str(e),
                    elapsed_ms=elapsed_ms,
                )
                # Graceful degrade — same reasoning as the timeout
                # branch. A broken summarizer can only cost an
                # uncompacted turn, never a stuck session.
                _COMPACTION_BREAKER.record_failure(time.monotonic())
                return None

            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result is None:
                _compaction_log.warning(
                    "compaction_failure model=%s reason=empty_summary elapsed_ms=%s",
                    self.model_id,
                    elapsed_ms,
                    extra={
                        "model_id": self.model_id,
                        "reason": "empty_summary",
                        "elapsed_ms": elapsed_ms,
                    },
                )
                emit_compaction_event(
                    "compaction_failure",
                    model_id=self.model_id,
                    reason="empty_summary",
                    elapsed_ms=elapsed_ms,
                )
                _COMPACTION_BREAKER.record_failure(time.monotonic())
                return None

            # Strip the <analysis> scratchpad / unwrap <summary>, then optionally
            # seed the user's durable memory into the summary (P3) — BEFORE
            # counting bytes, so the audit reflects what actually enters context.
            result = _strip_analysis(result)
            result = _seed_memory_into_summary(result, events)
            result = _frame_summary(result)  # continuation instruction, on top
            summary_bytes = _summary_bytes(result)
            if _compaction_log.isEnabledFor(logging.DEBUG):
                _compaction_log.debug(
                    "compaction_success model=%s summary_bytes=%s elapsed_ms=%s",
                    self.model_id,
                    summary_bytes,
                    elapsed_ms,
                    extra={
                        "model_id": self.model_id,
                        "summary_bytes": summary_bytes,
                        "elapsed_ms": elapsed_ms,
                    },
                )
            emit_compaction_event(
                "compaction_success",
                model_id=self.model_id,
                event_count=event_count,
                summary_bytes=summary_bytes,
                elapsed_ms=elapsed_ms,
            )
            _COMPACTION_BREAKER.record_success()
            return result

    return _LazyAdkCcSummarizer


def _summary_bytes(event) -> int:
    """Best-effort byte-count of the compaction summary text, for the
    `summary_bytes` field on `compaction_success`. Tolerates the
    various shapes ADK might return (Event with EventCompaction
    action, mock objects in tests)."""
    try:
        actions = getattr(event, "actions", None)
        if actions is None:
            return 0
        compaction = getattr(actions, "compaction", None)
        if compaction is None:
            return 0
        text = getattr(compaction, "compacted_content", None)
        if text is None:
            return 0
        if isinstance(text, str):
            return len(text.encode("utf-8"))
        # Fall back to repr for non-string payloads.
        return len(repr(text).encode("utf-8"))
    except Exception:
        return 0


def _make_compaction_summarizer():
    """Build a summarizer instance for `EventsCompactionConfig`.

    Always returns the lazy wrapper so audit + DEBUG hooks fire on
    every compaction call. Model id resolution:

      1. `ADK_CC_COMPACTION_MODEL` — explicit dedicated compaction model.
      2. Fall back to `ADK_CC_MODEL` — match the main-agent model so
         compaction uses the same backend.
      3. Last-resort `openai/gpt-4` — same fallback LiteLlm uses.

    api_base / api_key follow the same precedence: dedicated env var,
    then main-agent env var, then None / empty.

    Functionally equivalent to ADK's default summarizer
    (`_ensure_compaction_summarizer` auto-instantiating
    `LlmEventSummarizer(llm=agent.canonical_model)`) in the "no
    dedicated model" case, plus our observability hooks. The
    operator gets audit coverage just by enabling compaction.
    """
    model_id = (
        os.environ.get("ADK_CC_COMPACTION_MODEL")
        or os.environ.get("ADK_CC_MODEL")
        or "openai/gpt-4"
    )
    api_base = os.environ.get(
        "ADK_CC_COMPACTION_API_BASE", os.environ.get("ADK_CC_API_BASE")
    )
    api_key = os.environ.get(
        "ADK_CC_COMPACTION_API_KEY", os.environ.get("ADK_CC_API_KEY", "")
    )
    # Timeout (seconds) — 0 disables. See `_LazyAdkCcSummarizer`'s
    # docstring for the graceful-degrade contract. Default 30 protects
    # against hung summarizer LLMs without surprising fast paths.
    raw_timeout = os.environ.get("ADK_CC_COMPACTION_TIMEOUT_S", "30")
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        timeout_seconds = 30.0
    if timeout_seconds < 0:
        timeout_seconds = 0.0
    cls = _make_lazy_summarizer_class()
    return cls(
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        prompt_template=_resolve_compaction_prompt(),
        timeout_seconds=timeout_seconds,
    )


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
# CLI wire the plugin chain automatically. `PERMISSION_MODE` / `SETTINGS`
# are defined above (near tool instantiation) so plan-mode tools can use
# the env default for their internal mode check.

def _make_tenancy_plugin() -> TenancyPlugin:
    """Desktop mode (ADK_CC_DESKTOP=1) binds each session's workspace to a git
    worktree of its project — the resolver maps user_id (= project id) → repo →
    a per-session worktree. Otherwise the standard single-/multi-tenant behavior
    (workspace from ADK_CC_WORKSPACE_ROOT / CWD)."""
    if deployment.is_desktop():
        from .service.desktop_workspace import desktop_tenant_resolver

        return TenancyPlugin(tenant_resolver=desktop_tenant_resolver)
    return TenancyPlugin(default_workspace_root=os.environ.get("ADK_CC_WORKSPACE_ROOT"))


_app_kwargs = dict(
    name="adk_cc",
    root_agent=root_agent,
    # Order matters. Audit goes first so before_tool_callback records every
    # attempt — including ones the permission plugin denies. Permission's
    # short-circuit only stops the *chain*, but audit's row is already
    # written by then.
    plugins=[
        # Secret hygiene FIRST: its after_tool/after_model run before audit and
        # trace (registration order = execution order), so they scrub resolved
        # secret values out of tool results and model responses IN PLACE before
        # anything logs, persists, or delivers them. No before_* hooks, so this
        # doesn't disturb audit's "before_tool records first" property. Inert
        # when no CredentialProvider is configured.
        SecretRedactionPlugin(credential_provider_from_env()),
        AuditPlugin(),
        # Tenancy seeds state["temp:tenant_context"] / sandbox_workspace /
        # sandbox_backend before any tool fires AND calls
        # backend.ensure_workspace(ws) so remote-API backends (Daytona,
        # SandboxService) have a sandbox to talk to. Must sit before
        # PermissionPlugin so tenant context is in state when rules
        # evaluate. Reads ADK_CC_WORKSPACE_ROOT from env (or CWD when
        # unset for dev). Safe in single-user dev — degrades to
        # tenant_id="local", user_id="local".
        # Desktop mode binds the workspace to a per-session git worktree via the
        # resolver; otherwise standard single-/multi-tenant (ADK_CC_WORKSPACE_ROOT
        # / CWD). _make_tenancy_plugin's non-desktop branch == the prior explicit
        # TenancyPlugin(default_workspace_root=...).
        _make_tenancy_plugin(),
        # Graceful degradation for a model cut off mid tool-call: tolerant_tool_json
        # returns a marker (TRUNCATED_TOOL_CALL_KEY) for an unrecoverable truncation
        # instead of raising; this turns that marker into a clean retry error before
        # the tool runs — so a cutoff is a soft retry, not a turn crash. Before
        # AuthZ/Permission: no point gating/confirming a truncated call.
        TruncatedToolCallPlugin(),
        # AuthZ hard gate (subject×action×resource). Runs after Tenancy
        # (identity seeded) and BEFORE PermissionPlugin, so a hard deny
        # never reaches the confirmation prompt. Default-OFF: inert unless
        # ADK_CC_AUTHZ=1. Gates ALL tools incl. mcp__* (unlike Permission).
        AuthzPlugin(),
        PermissionPlugin(SETTINGS, default_mode=PERMISSION_MODE),
        # Per-tenant tool-call rate cap. Runs after Permission so a
        # denied call doesn't count against the quota.
        QuotaPlugin(
            calls_per_minute=int(
                os.environ.get("ADK_CC_QUOTA_PER_MINUTE", "120")
            )
        ),
        # Reminders run on before_model_callback, lifecycle independent of
        # the before_tool chain — order relative to others doesn't matter.
        # Pass the env-set default to every plugin that reads
        # `state["permission_mode"]` — without this, a fresh session
        # booted with `ADK_CC_PERMISSION_MODE=plan` has state=None at
        # the time these plugins fire, so they treat the session as
        # NORMAL mode (hiding `exit_plan_mode`, emitting task reminders,
        # not mentioning plan mode in error hints) — while
        # PermissionPlugin gates write/exec because IT correctly falls
        # back to its default. The result is a deadlock: write tools
        # blocked, no way to exit plan mode.
        # Auto-loads CLAUDE.md / .adk-cc/CONTEXT.md (project, tenant,
        # user, operator-extras) into the system_instruction. MUST
        # run BEFORE the reminder plugins below so the prepend lands
        # at the top of the system message and per-turn reminders
        # (plan mode, active tasks) append after — most-stable info
        # first, turn-volatile info last. Plugin no-ops when no
        # discoverable files exist OR when ADK_CC_DISABLE_PROJECT_CONTEXT=1.
        ProjectContextPlugin(),
        PlanModeReminderPlugin(default_mode=PERMISSION_MODE.value),
        TaskReminderPlugin(default_mode=PERMISSION_MODE.value),
        # Appends the resolved workspace directory to FS/exec tool
        # descriptions each turn so the model knows its working directory
        # and uses workspace-relative paths. Reads the per-session workspace
        # from state (or ADK_CC_WORKSPACE_ROOT / CWD). Disable with
        # ADK_CC_DISABLE_WORKSPACE_HINT=1.
        WorkspaceHintPlugin(),
        # Catches "tool not found" errors from ADK's tool dispatch and
        # turns them into corrective tool responses so the model can
        # self-correct on the next iteration instead of aborting the run.
        ToolCallValidatorPlugin(default_mode=PERMISSION_MODE.value),
        # Injects a UI-side response_schema into ask_user_question
        # function-call args so adk web's bundled UI renders a structured
        # form per question (instead of falling back to a free-form
        # textarea). after_model_callback runs after the LLM emits the
        # call but before ADK builds the event the UI consumes.
        # Force ask_user_question to actually PAUSE: if the model emits it
        # alongside other tool calls, drop the siblings so the ask is the sole
        # (long-running) call and the loop waits for the user's answer. Runs
        # before the UI-hint plugin so the surviving ask still gets its schema.
        AskPausePlugin(),
        AskUserQuestionUiHintPlugin(),
        # Rewrites adk_request_confirmation events so adk web's bundled
        # UI renders an N-option dropdown form instead of its hardcoded
        # binary checkbox widget. Inbound user submissions are reshaped
        # back to ADK's expected ToolConfirmation shape so the existing
        # request_confirmation resume processor handles them unchanged.
        # Disable to fall back to the binary widget.
        ConfirmationFormUiPlugin(),
        # Microcompaction (opt-in, ADK_CC_MICROCOMPACT=1): stub old, large
        # tool-result content in the outgoing request — the cheap, no-model
        # tier. Runs BEFORE ContextGuard so the shrink is reflected in its
        # token count (can defer WARN/REJECT and ADK's summarizer entirely).
        MicrocompactPlugin(),
        # Pre-flight context-length guardrail: WARN at 75% of MAX,
        # REJECT at 95%. ADK's EventsCompactionConfig (set above) is
        # the primary defense; this is the fail-soft safety net.
        ContextGuardPlugin(),
        # Auto-persists file-bearing content (embedded resource /
        # resource_link) returned by `mcp__*` tool calls into the artifact
        # store, so MCP export tools yield a downloadable artifact.
        # ENABLED BY DEFAULT; set ADK_CC_MCP_AUTOSAVE_EXPORTS=0 to disable
        # (then it's a single cheap gate check per tool call).
        # after_tool_callback overrides the result to strip inline bytes
        # once saved.
        McpExportArtifactPlugin(),
        # Raw model request/response trace for debugging model behavior.
        # Always registered; the plugin no-ops when `ADK_CC_LOG_MODEL_IO`
        # isn't `1`, so the per-turn cost is a single attribute check.
        # When enabled: DEBUG log line + `model_request`/`model_response`
        # audit events (when AuditPlugin's sink is also configured).
        # Placed at the END so before_model_callback captures the
        # FINAL LlmRequest (after ProjectContextPlugin, the reminder
        # plugins, and ContextGuardPlugin have all run). Without
        # this ordering the trace would miss prepended / appended
        # additions — misleading for "what did the model see?"
        # debugging. ContextGuard rejecting short-circuits, so the
        # trace only logs requests that actually went to the model.
        ModelIOTracePlugin(),
    ],
)
if _compaction_config is not None:
    _app_kwargs["events_compaction_config"] = _compaction_config

# Tool-call titles (opt-in, ADK_CC_TOOL_TITLES=1): the model labels each tool
# call ("Writing ML training script") for the frontend UI. Plugin-layer — adds
# an optional `title` arg to every tool declaration and strips it before
# execution; the recorded functionCall event keeps it for the UI. Appended
# last so injection runs after PlanModeReminderPlugin's tool filtering.
# Same flag also enables SESSION titles for the rail — an out-of-band LLM
# call after the first turn (SessionTitlePlugin), not a tool the agent must
# remember to call.
if os.environ.get("ADK_CC_TOOL_TITLES") == "1":
    from .plugins import SessionTitlePlugin, ToolTitlePlugin

    _app_kwargs["plugins"].append(ToolTitlePlugin())
    _app_kwargs["plugins"].append(SessionTitlePlugin())

# The wiki (ADK_CC_WIKI=1) is EXPLICIT — accessed via the wiki_search /
# wiki_read / wiki_add tools (wired onto the coordinator above). It has NO
# always-on recall plugin: autonomous recall/capture is the MEMORY system's
# job (below). Shared-domain merging is the offline librarian
# (scripts/wiki_librarian.py).

# Autonomous per-user memory (opt-in, ADK_CC_MEMORY=1). Always-injected
# budgeted recall (before_model, cheap) + full-turn capture of durable facts
# into episodic memory (after_run, one model call — captures the agent's
# output + tool results, not just the user message). Capture is on by default
# with the flag; ADK_CC_MEMORY_AUTOCAPTURE=0 disables it. Consolidation
# (episodic→semantic) is the separate scripts/memory_consolidator.py cron.
if os.environ.get("ADK_CC_MEMORY") == "1":
    from .plugins import MemoryPlugin

    _app_kwargs["plugins"].append(MemoryPlugin())

app = App(**_app_kwargs)
