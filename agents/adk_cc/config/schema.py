"""Central env-var schema — the single source of truth for `ADK_CC_*` config.

adk-cc has ~200 env vars historically read via scattered inline
`os.environ.get(...)` calls across ~58 files, documented by a hand-maintained
1100-line `.env.example` that drifts from the code. This module replaces that
with ONE typed schema: each var is a `Var` descriptor carrying its tier,
deployment profile, default, parser, and help text. From that schema we

  - **generate** `.env.example` (a short Quickstart + a full Reference), so the
    docs can never drift from the code again — `python -m adk_cc.config gen-env`;
  - **validate** a deployment's environment — `python -m adk_cc.config check`;
  - **report** effective values (secrets masked) — `python -m adk_cc.config print`.

Design notes (see analysis/env-var-refactor-plan.md):
  - Stdlib-only, no heavy imports — safe to import anywhere, including early boot.
  - `resolve(environ)` is a pure function (pass a dict; unit-testable).
  - **Phase 1 (this commit) is a faithful MIRROR of current behavior**, consumed
    only by the generator / `check` / `print` — NOT yet the source of truth for
    the app (modules still read `os.environ` directly). Converting readers to
    `config.get(...)` and deleting their inline reads happens per-cluster in a
    later phase, so this change is behavior-neutral. Defaults here MUST match the
    current inline defaults (guarded by tests/test_config_schema.py).
  - The field set below is being populated incrementally; it currently covers the
    Quickstart tiers plus a representative spread of every tier/profile/section.
    Remaining advanced/backend clusters are additive `Var(...)` rows.

Lives at `adk_cc/config/schema.py` (the `adk_cc.config` package also holds the
pre-existing `settings_loader`). CLI: `python -m adk_cc.config <check|print|gen-env>`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Tier(str, Enum):
    """How prominently a var surfaces to the operator."""

    REQUIRED = "required"   # no safe default; deployment (or a selected mode) breaks without it
    COMMON = "common"       # frequently set; has a default
    ADVANCED = "advanced"   # tuning knob with a sane default; belongs in the reference only
    DEV = "dev"             # dev / test / debug plumbing; not user-facing


class Profile(str, Enum):
    """Which deployment the var is relevant to (drives profile-scoped docs/check)."""

    ALL = "all"
    WEB = "web"             # multi-tenant web service only (inert in desktop)
    DESKTOP = "desktop"     # desktop app only


# --- parsers (raw str -> typed) -------------------------------------------
# `resolve` only calls a parser when the var is SET and non-empty, so parsers
# never see None/"". The `default` encodes the unset value.

def as_str(raw: str) -> str:
    return raw


def as_int(raw: str) -> int:
    return int(raw.strip())


def as_float(raw: str) -> float:
    return float(raw.strip())


def as_bool(raw: str) -> bool:
    """Truthy unless an explicit falsy token. Works for BOTH default-off opt-in
    flags and default-on kill switches — only the `default` differs (a kill
    switch has default=True and disables on '0'; an opt-in has default=False)."""
    return raw.strip().lower() not in ("0", "false", "no", "off")


def as_csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.replace(":", ",").split(",") if p.strip())


def as_path(raw: str) -> str:
    return os.path.abspath(os.path.expanduser(raw.strip()))


@dataclass(frozen=True)
class Var:
    """One `ADK_CC_*` environment variable."""

    name: str
    tier: Tier
    section: str                       # grouping in the generated reference (e.g. "Model", "Sandbox: Daytona")
    help: str                          # one/two-line description
    default: Any = None                # parsed value when unset (None = "off"/"unset")
    parse: Callable[[str], Any] = as_str
    profile: Profile = Profile.ALL
    example: Optional[str] = None       # sample value shown in the generated .env
    default_display: Optional[str] = None  # override the shown default (e.g. "off", "auto")
    secret: bool = False                # mask in `print`
    choices: Optional[tuple] = None     # allowed values (enum vars) — `check` rejects others
    # Optional conditional-requirement: given the resolved config dict, is this
    # var required? Used by `check` (e.g. DAYTONA_API_URL required iff backend=daytona).
    required_if: Optional[Callable[[dict], bool]] = field(default=None, compare=False)

    def resolve(self, environ: dict) -> Any:
        raw = environ.get(self.name)
        if raw is None or raw.strip() == "":
            return self.default
        try:
            return self.parse(raw)
        except Exception:
            return self.default  # tolerate garbage → default (check() flags it separately)

    def shown_default(self) -> str:
        if self.default_display is not None:
            return self.default_display
        if self.default is None:
            return "unset"
        if isinstance(self.default, bool):
            return "1" if self.default else "0"
        return str(self.default)


# ==========================================================================
# THE SCHEMA. Faithful mirror of current defaults (Phase 1). Add rows here as
# clusters are migrated; keep defaults == current inline defaults.
# ==========================================================================

FIELDS: list[Var] = [
    # --- Model (the one thing a user actually fills in) -------------------
    Var("ADK_CC_API_KEY", Tier.REQUIRED, "Model",
        "API key for your OpenAI-compatible model server.",
        secret=True, example="sk-replace-me"),
    Var("ADK_CC_API_BASE", Tier.COMMON, "Model",
        "OpenAI-compatible model server base URL.",
        default="http://localhost:18000/v1", parse=as_str),
    Var("ADK_CC_MODEL", Tier.COMMON, "Model",
        "LiteLLM model id (openai/<id>, ollama_chat/<id>, anthropic/<id>, …).",
        default="openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    Var("ADK_CC_MODEL_MAX_RPM", Tier.COMMON, "Model",
        "Throttle: max model-call starts per minute (paces all callers). Off by default.",
        default=None, parse=as_float, default_display="off", example="30"),
    Var("ADK_CC_MODEL_MIN_INTERVAL_S", Tier.ADVANCED, "Model",
        "Alternative to MAX_RPM: min seconds between model-call starts.",
        default=None, parse=as_float, default_display="off"),
    Var("ADK_CC_MAX_OUTPUT_TOKENS", Tier.ADVANCED, "Model",
        "Cap on model OUTPUT tokens/call (litellm max_tokens). 0 = uncapped; per-endpoint wins.",
        default=8192, parse=as_int),
    Var("ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED", Tier.ADVANCED, "Model",
        "Cap to escalate to after a mid-tool-call truncation (finish_reason=MAX_TOKENS). 0 disables.",
        default=32768, parse=as_int),
    Var("ADK_CC_MODEL_REGISTRY_FILE", Tier.ADVANCED, "Model",
        "Path to the live model-endpoint registry JSON (auto-defaulted when the admin panel is on).",
        default=None, parse=as_path, default_display="auto"),

    # --- Compaction / context (representative advanced knobs) ------------
    Var("ADK_CC_MAX_CONTEXT_TOKENS", Tier.COMMON, "Context",
        "Enable the context-guard ladder at this token budget. Unset = guard off.",
        default=None, parse=as_int, default_display="off"),
    Var("ADK_CC_COMPACTION_TOKEN_THRESHOLD", Tier.ADVANCED, "Context",
        "Token threshold that triggers conversation compaction. Requires EVENT_RETENTION set too.",
        default=None, parse=as_int, default_display="off"),
    Var("ADK_CC_COMPACTION_EVENT_RETENTION", Tier.ADVANCED, "Context",
        "Events to keep when compacting (must be set together with TOKEN_THRESHOLD).",
        default=None, parse=as_int, default_display="off"),
    Var("ADK_CC_MICROCOMPACT", Tier.ADVANCED, "Context",
        "Enable zero-cost tool-result eviction tier.",
        default=False, parse=as_bool),

    # --- Permissions -----------------------------------------------------
    Var("ADK_CC_PERMISSION_MODE", Tier.COMMON, "Permissions",
        "default | plan | acceptEdits | bypassPermissions | dontAsk. Dev default: bypassPermissions.",
        default="bypassPermissions",
        choices=("default","plan","acceptEdits","bypassPermissions","dontAsk")),
    Var("ADK_CC_PERMISSIONS_YAML", Tier.COMMON, "Permissions",
        "Path to a YAML of permission rules + authz policies.",
        default=None, parse=as_path),

    # --- Sandbox (selector + core; per-backend groups below) -------------
    Var("ADK_CC_SANDBOX_BACKEND", Tier.COMMON, "Sandbox",
        "noop | container | docker | e2b | sandbox_service | daytona | ssh. Default: noop (host exec).",
        default="noop",
        choices=("noop","container","docker","e2b","sandbox_service","daytona","ssh")),
    Var("ADK_CC_SANDBOX_IMAGE", Tier.ADVANCED, "Sandbox",
        "Container image for container/docker backends.",
        default="python:3.12-slim"),
    Var("ADK_CC_SANDBOX_NETWORK", Tier.ADVANCED, "Sandbox",
        "container backend: 1 = network on (dev), 0 = none.",
        default=True, parse=as_bool),
    Var("ADK_CC_SANDBOX_REQUIRE", Tier.ADVANCED, "Sandbox",
        "1 = fail closed: run_bash errors if the sandbox runtime is missing (no host fallback).",
        default=False, parse=as_bool),
    Var("ADK_CC_SANDBOX_ENV", Tier.COMMON, "Sandbox",
        "Static KEY=VALUE,… (or JSON) env baked into the sandbox for every command.",
        default=None),
    Var("ADK_CC_SANDBOX_ENV_PASSTHROUGH", Tier.COMMON, "Sandbox",
        "Host env var names to copy into the sandbox (comma-separated).",
        default=None, parse=as_csv),
    # Daytona (only when SANDBOX_BACKEND=daytona)
    Var("ADK_CC_DAYTONA_API_URL", Tier.REQUIRED, "Sandbox: Daytona",
        "Daytona control-plane URL.", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "daytona"),
    Var("ADK_CC_DAYTONA_API_KEY", Tier.REQUIRED, "Sandbox: Daytona",
        "Daytona API key (single-tenant) or use a credential provider.", default=None, secret=True,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "daytona"),
    Var("ADK_CC_DAYTONA_SNAPSHOT", Tier.COMMON, "Sandbox: Daytona",
        "Snapshot id/name to prewarm from (recommended).", default=None),
    # SSH remote workspace (only when SANDBOX_BACKEND=ssh via the env factory)
    Var("ADK_CC_SSH_HOST", Tier.REQUIRED, "Sandbox: SSH",
        "Remote host (alias or user@host) for the ssh backend env-factory path.", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "ssh"),
    Var("ADK_CC_SSH_WORKSPACE_PATH", Tier.REQUIRED, "Sandbox: SSH",
        "Absolute workspace path on the remote.", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "ssh"),

    # --- Memory / wiki (both master-flag OFF by default) -----------------
    Var("ADK_CC_MEMORY", Tier.ADVANCED, "Memory & Wiki",
        "Enable the autonomous memory subsystem (recall + capture).",
        default=False, parse=as_bool),
    Var("ADK_CC_WIKI", Tier.ADVANCED, "Memory & Wiki",
        "Enable the explicit wiki tools.",
        default=False, parse=as_bool),

    # --- Auth / web (multi-tenant web service only) ----------------------
    Var("ADK_CC_AUTH_PASSWORD", Tier.COMMON, "Auth (web)",
        "1 = use the in-house email+password identity service.",
        default=False, parse=as_bool, profile=Profile.WEB),
    Var("ADK_CC_JWT_JWKS_URL", Tier.COMMON, "Auth (web)",
        "External IdP JWKS URL (Keycloak/…); presence selects the JWT auth path.",
        default=None, profile=Profile.WEB),
    Var("ADK_CC_AUTHZ", Tier.COMMON, "Auth (web)",
        "Enable the ABAC authorization plugin + /authz routes.",
        default=False, parse=as_bool, profile=Profile.WEB),
    Var("ADK_CC_ADMIN_PANEL", Tier.COMMON, "Auth (web)",
        "Mount the admin panel routes.",
        default=False, parse=as_bool, profile=Profile.WEB),
    Var("ADK_CC_BOOTSTRAP_ADMIN_EMAIL", Tier.REQUIRED, "Auth (web)",
        "First-boot admin email (password mode). Inert after the first admin exists.",
        default=None, profile=Profile.WEB,
        required_if=lambda c: bool(c.get("ADK_CC_AUTH_PASSWORD"))),
    Var("ADK_CC_BOOTSTRAP_ADMIN_PASSWORD", Tier.REQUIRED, "Auth (web)",
        "First-boot admin password (with EMAIL).",
        default=None, secret=True, profile=Profile.WEB,
        required_if=lambda c: bool(c.get("ADK_CC_AUTH_PASSWORD"))),
    Var("ADK_CC_ALLOW_NO_AUTH", Tier.COMMON, "Auth (web)",
        "Explicitly run with NO auth (fail-open). Desktop sets this; web dev only.",
        default=False, parse=as_bool),

    # --- Deployment / storage / paths ------------------------------------
    Var("ADK_CC_WORKSPACE_ROOT", Tier.COMMON, "Deployment",
        "Web/multi-tenant workspace anchor. Unset = CWD (dev zero-config).",
        default=None, parse=as_path, default_display="CWD"),
    Var("ADK_CC_DATA_DIR", Tier.COMMON, "Deployment",
        "Server-side data root — identity, admin/tenant registry, credentials, "
        "audit, tasks, codex token all default under here.",
        default=None, parse=as_path, default_display="<cwd>/.adk-cc (web) / desktop data dir"),
    Var("ADK_CC_SESSION_DSN", Tier.COMMON, "Deployment",
        "Persistent session store DSN (postgres/sqlite). Unset = in-memory. Ignored in desktop.",
        default=None, profile=Profile.WEB, default_display="in-memory"),
    Var("ADK_CC_ARTIFACT_STORAGE_URI", Tier.COMMON, "Deployment",
        "Artifact persistence URI (file://, gs://, s3://). Unset = in-memory.",
        default=None, default_display="in-memory"),
    Var("ADK_CC_DESKTOP_DATA", Tier.ADVANCED, "Deployment",
        "Desktop data root (sessions, settings, secrets, checkpoints).",
        default=None, parse=as_path, default_display="~/.adk-cc-desktop", profile=Profile.DESKTOP),
    Var("ADK_CC_AGENTS_DIR", Tier.COMMON, "Deployment",
        "Agents root ADK discovers the agent package under (defaults to this package's agents/ dir).",
        default=None, parse=as_path, default_display="<package>/agents"),
    Var("ADK_CC_SERVE_UI", Tier.ADVANCED, "Deployment",
        "Mount the SPA (set by the desktop app / UI deployments).",
        default=False, parse=as_bool),
    Var("ADK_CC_LOG_LEVEL", Tier.COMMON, "Deployment",
        "Log verbosity (DEBUG/INFO/WARNING/…).", default="INFO"),
    Var("ADK_CC_LOG_FORMAT", Tier.COMMON, "Deployment",
        "Log format: text | json.", default="text", choices=("text","json")),

    # --- Misc feature flags ----------------------------------------------
    Var("ADK_CC_TOLERANT_TOOL_JSON", Tier.ADVANCED, "Behavior",
        "Default-on repair of malformed tool-call JSON (set 0 to disable).",
        default=True, parse=as_bool),
    Var("ADK_CC_CHECKPOINT", Tier.ADVANCED, "Behavior",
        "Shadow-git checkpoint/undo (default-on in desktop; set 0 to disable).",
        default=True, parse=as_bool),
    Var("ADK_CC_WEB_FETCH_MODE", Tier.ADVANCED, "Behavior",
        "web_fetch host policy: open (SSRF-guarded) | allowlist.", default="open",
        choices=("open","allowlist")),
    Var("ADK_CC_SKIP_DOTENV", Tier.DEV, "Behavior",
        "Skip .env loading (CI/containers where env is already populated).",
        default=False, parse=as_bool),

    # ===================== remaining coverage =========================

    # --- Model: Codex + compaction fallbacks -----------------------------
    Var("ADK_CC_CODEX_STORE_DIR", Tier.ADVANCED, "Model: Codex",
        "Where adk-cc stores its own Codex OAuth token.", default=None, parse=as_path,
        default_display="<DATA_DIR>"),
    Var("ADK_CC_CODEX_AUTH_FILE", Tier.ADVANCED, "Model: Codex",
        "Explicit Codex auth.json path (else ~/.codex/auth.json).", default=None, parse=as_path),
    Var("ADK_CC_CODEX_EFFORT", Tier.ADVANCED, "Model: Codex",
        "Codex reasoning-effort fallback (per-endpoint wins).", default="medium"),
    Var("ADK_CC_CODEX_BASE_URL", Tier.DEV, "Model: Codex",
        "Codex backend URL (dev/mock override; read at import).",
        default="https://chatgpt.com/backend-api/codex"),
    Var("ADK_CC_COMPACTION_MODEL", Tier.COMMON, "Context",
        "Model for conversation summarization (falls back to the main model).", default=None,
        default_display="$ADK_CC_MODEL"),
    Var("ADK_CC_COMPACTION_API_BASE", Tier.ADVANCED, "Context",
        "API base for the compaction model (different provider).", default=None,
        default_display="$ADK_CC_API_BASE"),
    Var("ADK_CC_COMPACTION_API_KEY", Tier.ADVANCED, "Context", secret=True,
        help="API key for the compaction model.", default=None, default_display="$ADK_CC_API_KEY"),
    Var("ADK_CC_COMPACTION_TIMEOUT_S", Tier.ADVANCED, "Context",
        "Summarizer timeout seconds (0 = none).", default=30, parse=as_int),
    Var("ADK_CC_COMPACTION_SEED_MEMORY", Tier.ADVANCED, "Context",
        "Seed memory stores into the compaction summary (opt-in).", default=False, parse=as_bool),
    Var("ADK_CC_COMPACTION_SEED_BUDGET", Tier.ADVANCED, "Context",
        "Token budget for seeded memory.", default=300, parse=as_int),
    Var("ADK_CC_COMPACTION_FRAME", Tier.ADVANCED, "Context",
        "Compaction framing line; 0 disables, a string overrides the built-in.", default=None,
        default_display="built-in"),
    Var("ADK_CC_COMPACTION_BREAKER_THRESHOLD", Tier.ADVANCED, "Context",
        "Compaction circuit-breaker failures before tripping (0 disables).", default=3, parse=as_int),
    Var("ADK_CC_COMPACTION_BREAKER_COOLDOWN_S", Tier.ADVANCED, "Context",
        "Circuit-breaker cooldown seconds.", default=60, parse=as_int),
    Var("ADK_CC_COMPACTION_PROMPT", Tier.ADVANCED, "Context",
        "Inline compaction prompt override.", default=None, default_display="built-in template"),
    Var("ADK_CC_COMPACTION_PROMPT_FILE", Tier.ADVANCED, "Context",
        "Path to a compaction prompt template.", default=None, parse=as_path),
    Var("ADK_CC_COMPACTION_OVERLAP", Tier.ADVANCED, "Context",
        "Event overlap when compacting.", default=2, parse=as_int),
    Var("ADK_CC_COMPACTION_INTERVAL", Tier.ADVANCED, "Context",
        "Event-count compaction interval (also an enable path).", default=10, parse=as_int),
    Var("ADK_CC_CONTEXT_FILES_MAX_BYTES", Tier.ADVANCED, "Context",
        "Per-file cap for CLAUDE.md/AGENTS.md/CONTEXT.md project-context injection.",
        default=50000, parse=as_int),
    Var("ADK_CC_CONTEXT_WARN_TOKENS", Tier.ADVANCED, "Context",
        "Context-guard warn threshold (derived ~75% if unset).", default=None, parse=as_int,
        default_display="derived"),
    Var("ADK_CC_CONTEXT_RESERVE_TOKENS", Tier.ADVANCED, "Context",
        "Output-headroom reserve for the context guard.", default=0, parse=as_int),
    Var("ADK_CC_CONTEXT_REJECT_TOKENS", Tier.ADVANCED, "Context",
        "Context-guard hard-stop threshold (derived ~95% if unset).", default=None, parse=as_int,
        default_display="derived"),
    Var("ADK_CC_CONTEXT_COUNT_TOOL_PAYLOADS", Tier.ADVANCED, "Context",
        "Count tool payloads in context sizing (opt-in).", default=False, parse=as_bool),
    Var("ADK_CC_CONTEXT_FILES", Tier.ADVANCED, "Context",
        "Extra absolute context files (comma-separated) layered on discovery.",
        default=None, parse=as_csv),
    Var("ADK_CC_MICROCOMPACT_MIN_TOKENS", Tier.ADVANCED, "Context",
        "Microcompact size floor.", default=800, parse=as_int),
    Var("ADK_CC_MICROCOMPACT_KEEP_RECENT", Tier.ADVANCED, "Context",
        "Microcompact recency window.", default=4, parse=as_int),

    # --- Sandbox: shared + container + noop ------------------------------
    Var("ADK_CC_SANDBOX_MODE", Tier.ADVANCED, "Sandbox",
        "Desktop sandbox mode: host | container (usually set via the UI).", default="host",
        profile=Profile.DESKTOP, choices=("host","container")),
    Var("ADK_CC_SANDBOX_RUNTIME", Tier.ADVANCED, "Sandbox",
        "Container runtime: auto | docker | podman.", default="auto",
        choices=("auto","docker","podman")),
    Var("ADK_CC_SANDBOX_IDLE_TTL_S", Tier.ADVANCED, "Sandbox",
        "Reap idle session containers after N seconds (0 = off).", default=0, parse=as_int),
    Var("ADK_CC_SANDBOX_MEM_LIMIT", Tier.ADVANCED, "Sandbox",
        "Container memory limit.", default="4g"),
    Var("ADK_CC_SANDBOX_CPUS", Tier.ADVANCED, "Sandbox",
        "Container CPU limit (container backend).", default="2"),
    Var("ADK_CC_SANDBOX_CPU_QUOTA", Tier.ADVANCED, "Sandbox",
        "Docker CPU quota (=1 CPU; docker backend).", default=100000, parse=as_int),
    Var("ADK_CC_SANDBOX_PIDS_LIMIT", Tier.ADVANCED, "Sandbox",
        "Container PID limit (docker backend defaults 256).", default=512, parse=as_int),
    Var("ADK_CC_BASH_STREAM", Tier.ADVANCED, "Sandbox",
        "Stream run_bash output to operator logs.", default=False, parse=as_bool),
    Var("ADK_CC_NOOP_ACK_HOST_EXEC", Tier.DEV, "Sandbox",
        "Ack host exec on prod-shaped paths (noop backend).", default=None, parse=as_bool,
        default_display="is_desktop()"),
    Var("ADK_CC_SANDBOX_ENV_CREDENTIALS", Tier.ADVANCED, "Sandbox",
        "ENV_NAME=credential_key,… resolved per-tenant into the sandbox.", default=None),
    # Sandbox: Docker
    Var("ADK_CC_DOCKER_HOST", Tier.REQUIRED, "Sandbox: Docker",
        "Docker daemon (tcp://…, ssh://…).", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "docker"),
    Var("ADK_CC_DOCKER_CA_CERT", Tier.ADVANCED, "Sandbox: Docker",
        "mTLS CA cert path (all three docker certs together).", default=None, parse=as_path),
    Var("ADK_CC_DOCKER_CLIENT_CERT", Tier.ADVANCED, "Sandbox: Docker",
        "mTLS client cert path.", default=None, parse=as_path),
    Var("ADK_CC_DOCKER_CLIENT_KEY", Tier.ADVANCED, "Sandbox: Docker",
        "mTLS client key path.", default=None, parse=as_path),
    Var("ADK_CC_DISABLE_INSTALL_CACHE_MOUNT", Tier.ADVANCED, "Sandbox: Docker",
        "Disable the shared install-cache mount (docker backend).", default=False, parse=as_bool),
    # Sandbox: Daytona (remaining)
    Var("ADK_CC_DAYTONA_PROXY_URL", Tier.COMMON, "Sandbox: Daytona",
        "Toolbox proxy base (derived :3000→:4000 if unset).", default=None, default_display="derived"),
    Var("ADK_CC_DAYTONA_WORKSPACE_PATH", Tier.ADVANCED, "Sandbox: Daytona",
        "In-sandbox workspace path.", default="/home/daytona"),
    Var("ADK_CC_DAYTONA_AUTOSTOP_MIN", Tier.ADVANCED, "Sandbox: Daytona",
        "Auto-stop after N idle minutes.", default=15, parse=as_int),
    Var("ADK_CC_DAYTONA_AUTODELETE_MIN", Tier.ADVANCED, "Sandbox: Daytona",
        "Auto-delete after N minutes.", default=1440, parse=as_int),
    Var("ADK_CC_DAYTONA_DELETE_ON_CLOSE", Tier.ADVANCED, "Sandbox: Daytona",
        "DELETE the sandbox on close instead of stop.", default=False, parse=as_bool),
    Var("ADK_CC_DAYTONA_START_TIMEOUT_S", Tier.ADVANCED, "Sandbox: Daytona",
        "Sandbox start poll timeout.", default=120.0, parse=as_float),
    Var("ADK_CC_DAYTONA_REQUEST_TIMEOUT_S", Tier.ADVANCED, "Sandbox: Daytona",
        "Per-request timeout.", default=30.0, parse=as_float),
    Var("ADK_CC_DAYTONA_CREDENTIAL_KEY", Tier.ADVANCED, "Sandbox: Daytona",
        "Credential key for per-tenant Daytona token lookup.", default="daytona_api_key"),
    Var("ADK_CC_DAYTONA_VERIFY_SSL", Tier.ADVANCED, "Sandbox: Daytona",
        "Verify TLS (0 for a self-signed dev plane).", default=True, parse=as_bool),
    Var("ADK_CC_DAYTONA_CA_BUNDLE", Tier.ADVANCED, "Sandbox: Daytona",
        "CA bundle for a private Daytona CA.", default=None, parse=as_path),
    Var("ADK_CC_DAYTONA_CREATE_MAX_ATTEMPTS", Tier.ADVANCED, "Sandbox: Daytona",
        "Capacity-backoff attempt cap.", default=6, parse=as_int),
    Var("ADK_CC_DAYTONA_CREATE_TOTAL_WAIT_S", Tier.ADVANCED, "Sandbox: Daytona",
        "Capacity-backoff total wall-clock cap.", default=45.0, parse=as_float),
    # Sandbox: service
    Var("ADK_CC_SANDBOX_SERVICE_URL", Tier.REQUIRED, "Sandbox: service",
        "External sandbox REST service URL.", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "sandbox_service"),
    Var("ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN", Tier.REQUIRED, "Sandbox: service", secret=True,
        help="Shared bearer token (single-tenant) or use a credential provider.", default=None,
        required_if=lambda c: c.get("ADK_CC_SANDBOX_BACKEND") == "sandbox_service"),
    Var("ADK_CC_SANDBOX_SERVICE_TOKEN_KEY", Tier.ADVANCED, "Sandbox: service",
        "Credential key for per-tenant token lookup.", default="sandbox_service_token"),
    Var("ADK_CC_SANDBOX_SERVICE_VCPU", Tier.ADVANCED, "Sandbox: service",
        "Requested vCPU (service caps).", default=None, parse=as_int),
    Var("ADK_CC_SANDBOX_SERVICE_MEMORY_GIB", Tier.ADVANCED, "Sandbox: service",
        "Requested memory GiB.", default=None, parse=as_int),
    Var("ADK_CC_SANDBOX_SERVICE_WORKSPACE_GIB", Tier.ADVANCED, "Sandbox: service",
        "Requested workspace GiB.", default=None, parse=as_int),
    Var("ADK_CC_SANDBOX_SERVICE_EXEC_TIMEOUT_S", Tier.ADVANCED, "Sandbox: service",
        "Per-exec timeout.", default=None, parse=as_int),
    Var("ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S", Tier.ADVANCED, "Sandbox: service",
        "Hard-destroy TTL (service default 86400).", default=None, parse=as_int),
    Var("ADK_CC_SANDBOX_SERVICE_VERIFY_TLS", Tier.ADVANCED, "Sandbox: service",
        "Verify TLS (0 for dev).", default=True, parse=as_bool),
    # Sandbox: SSH (remaining)
    Var("ADK_CC_SSH_PORT", Tier.ADVANCED, "Sandbox: SSH",
        "Remote SSH port.", default=None, parse=as_int, default_display="22"),
    Var("ADK_CC_SSH_IDENTITY_FILE", Tier.ADVANCED, "Sandbox: SSH",
        "SSH key path (else ~/.ssh/config / agent).", default=None, parse=as_path),
    Var("ADK_CC_SSH_EXTRA_OPTS", Tier.DEV, "Sandbox: SSH",
        "Extra ssh opts (tests use this for throwaway known_hosts).", default=None),
    Var("ADK_CC_SSH_CONTROL_DIR", Tier.ADVANCED, "Sandbox: SSH",
        "ControlMaster socket dir.", default=None, parse=as_path, default_display="~/.adk-cc-ssh"),

    # --- Auth (web) ------------------------------------------------------
    Var("ADK_CC_AUTH_TOKENS", Tier.DEV, "Auth (web)", secret=True, profile=Profile.WEB,
        help="Static tok=user:tenant[:role] map (dev; use JWT for prod).", default=None),
    Var("ADK_CC_AUTH_TOKEN_TTL_S", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Access-token TTL.", default=1800, parse=as_int),
    Var("ADK_CC_AUTH_REFRESH_TTL_S", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Refresh-token TTL.", default=2592000, parse=as_int),
    Var("ADK_CC_AUTH_RESET_TTL_S", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Password-reset link TTL.", default=86400, parse=as_int),
    Var("ADK_CC_AUTH_RATELIMIT", Tier.DEV, "Auth (web)", profile=Profile.WEB,
        help="Enable /auth/* rate limiting (test kill switch).", default=True, parse=as_bool),
    Var("ADK_CC_AUTH_RATELIMIT_WINDOW_S", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Rate-limit sliding window.", default=60, parse=as_int),
    Var("ADK_CC_AUTH_RATELIMIT_MAX", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Max /auth/* requests per window.", default=30, parse=as_int),
    Var("ADK_CC_AUTH_LOCKOUT_THRESHOLD", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Failures before lockout.", default=5, parse=as_int),
    Var("ADK_CC_AUTH_LOCKOUT_S", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Lockout duration.", default=300, parse=as_int),
    Var("ADK_CC_AUTH_ISSUER", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="In-house token issuer (iss).", default="adk-cc"),
    Var("ADK_CC_AUTH_AUDIENCE", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="In-house token audience (aud).", default=None),
    Var("ADK_CC_JWT_ISSUER", Tier.COMMON, "Auth (web)", profile=Profile.WEB,
        help="Expected iss from the external IdP (unset = not verified).", default=None),
    Var("ADK_CC_JWT_AUDIENCE", Tier.COMMON, "Auth (web)", profile=Profile.WEB,
        help="Expected aud from the external IdP.", default=None),
    Var("ADK_CC_JWT_USER_CLAIM", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Claim mapping: user id.", default="sub"),
    Var("ADK_CC_JWT_TENANT_CLAIM", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Claim mapping: tenant.", default="tenant"),
    Var("ADK_CC_JWT_SCOPES_CLAIM", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Claim mapping: scopes.", default="scope"),
    Var("ADK_CC_JWT_ROLES_CLAIM", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Claim mapping: roles.", default="roles"),
    Var("ADK_CC_ADMIN_ROLE", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Role name for the admin guard.", default="admin"),
    Var("ADK_CC_ACCESS_REQUESTS", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Unknown-email logins become approvable access requests.", default=True, parse=as_bool),
    Var("ADK_CC_TENANCY_MODE", Tier.COMMON, "Auth (web)", profile=Profile.WEB,
        help="single | multi org model.", default="single", choices=("single","multi")),
    Var("ADK_CC_GLOBAL_TENANT_ID", Tier.ADVANCED, "Auth (web)", profile=Profile.WEB,
        help="Tenant id for single mode / admin global tenant.", default="local"),
    Var("ADK_CC_TRUST_PROXY", Tier.COMMON, "Auth (web)", profile=Profile.WEB,
        help="Trust X-Forwarded-For for ratelimit/lockout IP (behind a proxy).",
        default=False, parse=as_bool),

    # --- Credentials / identity / audit (both modes unless noted) --------
    Var("ADK_CC_CREDENTIAL_PROVIDER", Tier.COMMON, "Credentials",
        "memory | encrypted_file | none.", default="memory",
        choices=("memory","encrypted_file","none")),
    Var("ADK_CC_CREDENTIAL_STORE_DIR", Tier.COMMON, "Credentials",
        "Secret store dir (defaults to <DATA_DIR>/secrets for encrypted_file).",
        default=None, parse=as_path, default_display="<DATA_DIR>/secrets"),
    Var("ADK_CC_CREDENTIAL_KEY", Tier.REQUIRED, "Credentials", secret=True,
        help="Fernet key for the encrypted secret store.", default=None,
        required_if=lambda c: c.get("ADK_CC_CREDENTIAL_PROVIDER") == "encrypted_file"),
    Var("ADK_CC_IDENTITY_DIR", Tier.ADVANCED, "Deployment", profile=Profile.WEB,
        help="Identity store dir (users/invites/keys/audit).", default=None, parse=as_path,
        default_display="<DATA_DIR>/identity"),
    Var("ADK_CC_ADMIN_DATA_DIR", Tier.COMMON, "Deployment", profile=Profile.WEB,
        help="Admin/tenant data root (when the admin panel is on).", default=None, parse=as_path,
        default_display="<DATA_DIR>/admin-data"),
    Var("ADK_CC_TENANT_REGISTRY_DIR", Tier.ADVANCED, "Deployment", profile=Profile.WEB,
        help="Per-tenant MCP registry dir.", default=None, parse=as_path,
        default_display="<admin-data>/registry"),
    Var("ADK_CC_TENANT_SKILLS_DIR", Tier.ADVANCED, "Deployment", profile=Profile.WEB,
        help="Per-tenant skills dir.", default=None, parse=as_path,
        default_display="<admin-data>/skills"),
    Var("ADK_CC_SECRET_REDACTION_TTL_S", Tier.ADVANCED, "Behavior",
        "Secret-value cache TTL in the redaction plugin.", default=15.0, parse=as_float),
    Var("ADK_CC_AUDIT_LOG", Tier.COMMON, "Deployment",
        "Tool/permission audit JSONL sink.", default=None, parse=as_path,
        default_display="<DATA_DIR>/audit.jsonl"),
    Var("ADK_CC_QUOTA_PER_MINUTE", Tier.ADVANCED, "Behavior",
        "Per-tenant tool-call cap per minute.", default=120, parse=as_int),

    # --- Memory & Wiki (all inert unless the master flag is on) ----------
    Var("ADK_CC_MEMORY_ROOT", Tier.COMMON, "Memory & Wiki",
        "Memory store dir.", default=None, parse=as_path, default_display="<workspace>/.memory"),
    Var("ADK_CC_MEMORY_STORE_URI", Tier.ADVANCED, "Memory & Wiki",
        "Memory store URI (only file:// implemented — prefer MEMORY_ROOT).", default=None),
    Var("ADK_CC_MEMORY_SYNTH", Tier.ADVANCED, "Memory & Wiki",
        "LLM consolidation unless set to 'deterministic'.", default=None, default_display="llm"),
    Var("ADK_CC_MEMORY_AUTOCAPTURE", Tier.ADVANCED, "Memory & Wiki",
        "Capture memories after turns (0 = recall-only).", default=True, parse=as_bool),
    Var("ADK_CC_MEMORY_RESOLVE", Tier.ADVANCED, "Memory & Wiki",
        "Identity-resolution model call (0 disables).", default=True, parse=as_bool),
    Var("ADK_CC_MEMORY_RESOLVE_VERIFY", Tier.ADVANCED, "Memory & Wiki",
        "Verify resolution (sub-knob of RESOLVE).", default=True, parse=as_bool),
    Var("ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD", Tier.COMMON, "Memory & Wiki",
        "Responsive consolidation trigger (unset = off).", default=None, parse=as_int, default_display="off"),
    Var("ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S", Tier.COMMON, "Memory & Wiki",
        "In-process periodic consolidation interval (≤0 = off).", default=None, parse=as_int, default_display="off"),
    Var("ADK_CC_MEMORY_CONSOLIDATE_DELAY_S", Tier.DEV, "Memory & Wiki",
        "Scheduler boot-settle delay (test hook).", default=60, parse=as_int),
    Var("ADK_CC_MEMORY_STALE_DAYS", Tier.ADVANCED, "Memory & Wiki",
        "Archive memories older than N days.", default=90, parse=as_int),
    Var("ADK_CC_MEMORY_COMPACT", Tier.ADVANCED, "Memory & Wiki",
        "LLM compaction pass in the scheduler (0 disables).", default=True, parse=as_bool),
    Var("ADK_CC_MEMORY_EPISODIC_CAP", Tier.ADVANCED, "Memory & Wiki",
        "Episodic retention cap (0 = keep all).", default=0, parse=as_int, default_display="keep all"),
    Var("ADK_CC_MEMORY_RECALL_BUDGET_TOKENS", Tier.ADVANCED, "Memory & Wiki",
        "Recall prompt budget.", default=600, parse=as_int),
    Var("ADK_CC_MEMORY_CAPTURE_TIMEOUT_S", Tier.ADVANCED, "Memory & Wiki",
        "Capture model-call timeout.", default=30.0, parse=as_float),
    Var("ADK_CC_WIKI_ROOT", Tier.COMMON, "Memory & Wiki",
        "Wiki store dir.", default=None, parse=as_path, default_display="<workspace>/.wiki"),
    Var("ADK_CC_WIKI_STORE_URI", Tier.ADVANCED, "Memory & Wiki",
        "Wiki store URI (only file:// implemented — prefer WIKI_ROOT).", default=None),
    Var("ADK_CC_WIKI_CORROBORATION_N", Tier.ADVANCED, "Memory & Wiki",
        "Corroborations before a wiki fact is trusted (admin API overrides).", default=2, parse=as_int),

    # --- Permissions / safety --------------------------------------------
    Var("ADK_CC_SETTINGS_FILE", Tier.ADVANCED, "Deployment", profile=Profile.DESKTOP,
        help="Desktop settings.env path override.", default=None, parse=as_path),
    Var("ADK_CC_PROTECTED_DENY", Tier.ADVANCED, "Permissions",
        "Extra hard-deny path patterns (additive to the built-in secret floor).", default=None, parse=as_csv),
    Var("ADK_CC_PROTECTED_ASK", Tier.ADVANCED, "Permissions",
        "Extra always-ask path patterns.", default=None, parse=as_csv),
    Var("ADK_CC_DANGEROUS_CMDS", Tier.ADVANCED, "Permissions",
        "Extra 'dangerous' command basenames (always-ask).", default=None, parse=as_csv),
    Var("ADK_CC_CATASTROPHIC_CMDS", Tier.ADVANCED, "Permissions",
        "Extra 'catastrophic' command basenames (hard-deny).", default=None, parse=as_csv),
    Var("ADK_CC_CMD_SAFETY", Tier.ADVANCED, "Permissions",
        "run_bash danger classifier (set 0 to disable entirely).", default=True, parse=as_bool),

    # --- MCP (servers adk-cc CONNECTS to) --------------------------------
    Var("ADK_CC_MCP_SERVERS_FILE", Tier.COMMON, "MCP",
        "JSON array of MCP servers to connect to (per-entry name/transport/filters/creds).",
        default=None, parse=as_path),
    Var("ADK_CC_MCP_SERVER", Tier.ADVANCED, "MCP",
        "Single MCP server (legacy; prefer SERVERS_FILE).", default=None),
    Var("ADK_CC_MCP_SERVER_NAME", Tier.ADVANCED, "MCP",
        "Tool prefix for the single env server.", default="mcp"),
    Var("ADK_CC_MCP_TRANSPORT", Tier.ADVANCED, "MCP",
        "Transport for the single env server: stdio | sse | http.", default="stdio",
        choices=("stdio","sse","http")),
    Var("ADK_CC_MCP_USE_RESOURCES", Tier.ADVANCED, "MCP",
        "Expose MCP resources for the single env server.", default=False, parse=as_bool),
    Var("ADK_CC_MCP_SAVE_RESOURCES_AS_ARTIFACTS", Tier.ADVANCED, "MCP",
        "Save MCP resources as artifacts (single env server).", default=False, parse=as_bool),
    Var("ADK_CC_MCP_AUTOSAVE_EXPORTS", Tier.ADVANCED, "MCP",
        "Auto-persist MCP file results (set 0 to disable).", default=True, parse=as_bool),
    Var("ADK_CC_MCP_AUTOSAVE_AUDIENCE_USER_ONLY", Tier.ADVANCED, "MCP",
        "Autosave only user-audience blobs (sub-knob).", default=True, parse=as_bool),

    # --- Skills ----------------------------------------------------------
    Var("ADK_CC_SKILLS_DIR", Tier.COMMON, "Skills",
        "Explicit skills root (shadows project/install skills).", default=None, parse=as_path),
    Var("ADK_CC_SKILL_GUARDS", Tier.ADVANCED, "Skills",
        "Skill safety guards (untrusted-content wrapping + host-exec refusal).", default=False, parse=as_bool),
    Var("ADK_CC_SKILL_SCRIPTS_ACK_HOST_EXEC", Tier.DEV, "Skills",
        "Ack running skill scripts on the host under noop backend.", default=False, parse=as_bool),
    Var("ADK_CC_SKILL_RESOURCE_READ_MAX_BYTES", Tier.ADVANCED, "Skills",
        "Per-file disk-read cap for skill resources.", default=4194304, parse=as_int),
    Var("ADK_CC_SKILL_RESOURCE_MAX_LINES", Tier.ADVANCED, "Skills",
        "Cap on load_skill_resource line limit.", default=400, parse=as_int),
    Var("ADK_CC_SKILL_RESOURCE_DEFAULT_LINES", Tier.ADVANCED, "Skills",
        "Default load_skill_resource lines.", default=200, parse=as_int),
    Var("ADK_CC_SKILL_FILE_MAX_BYTES", Tier.ADVANCED, "Skills",
        "Boot-time RAM guard: prune oversize skill references.", default=262144, parse=as_int),
    Var("ADK_CC_SKILL_INSTRUCTIONS_MAX_CHARS", Tier.ADVANCED, "Skills",
        "Truncation cap on injected SKILL.md instructions.", default=60000, parse=as_int),
    Var("ADK_CC_DISABLE_PROJECT_SKILLS", Tier.ADVANCED, "Skills",
        "Disable the project .adk-cc/skills walk-up.", default=False, parse=as_bool),

    # --- Deployment / storage / misc (remaining) -------------------------
    Var("ADK_CC_DESKTOP", Tier.DEV, "Deployment",
        "Desktop-mode selector (set by the app, not users).", default=False, parse=as_bool),
    Var("ADK_CC_TASKS_DIR", Tier.ADVANCED, "Deployment",
        "Central task storage override.", default=None, parse=as_path,
        default_display="<workspace>/.adk-cc/tasks or <DATA_DIR>/tasks"),
    Var("ADK_CC_SESSION_RETRY_ON_STALE", Tier.ADVANCED, "Behavior",
        "Opt-in retry around an upstream ADK stale-session race in HITL.", default=False, parse=as_bool),
    Var("ADK_CC_S3_ENDPOINT_URL", Tier.ADVANCED, "Deployment",
        "S3 endpoint for s3:// artifacts (falls back to AWS_ENDPOINT_URL).", default=None),
    Var("ADK_CC_UI_DIST", Tier.ADVANCED, "Deployment",
        "Built SPA dir to serve.", default=None, parse=as_path, default_display="<repo>/web/dist"),
    Var("ADK_CC_KNOWLEDGE_UI", Tier.ADVANCED, "Deployment",
        "Experimental knowledge-graph endpoints.", default=False, parse=as_bool),
    Var("ADK_CC_LOG_MODEL_IO", Tier.DEV, "Deployment",
        "Dump raw LLM request/response to DEBUG log.", default=False, parse=as_bool),
    Var("ADK_CC_LOG_MODEL_IO_MAX_BYTES", Tier.DEV, "Deployment",
        "Truncation cap for LOG_MODEL_IO.", default=50000, parse=as_int),
    Var("ADK_CC_TOOL_TITLES", Tier.ADVANCED, "Behavior",
        "Generate tool/session titles (costs extra model tokens).", default=False, parse=as_bool),
    Var("ADK_CC_TASK_REMINDER", Tier.ADVANCED, "Behavior",
        "Periodic task-list nudge (set 0 to disable).", default=True, parse=as_bool),
    Var("ADK_CC_WEB_FETCH_ALLOW_PRIVATE", Tier.DEV, "Behavior",
        "Disable the SSRF guard so localhost can be fetched (security-sensitive).", default=False, parse=as_bool),
    Var("ADK_CC_WEB_FETCH_HOSTS", Tier.ADVANCED, "Behavior",
        "Extra allowlisted fetch hosts (with WEB_FETCH_MODE=allowlist).", default=None, parse=as_csv),
    Var("ADK_CC_DISABLE_WORKSPACE_HINT", Tier.ADVANCED, "Behavior",
        "Disable the per-turn workspace-path system hint.", default=False, parse=as_bool),
    Var("ADK_CC_DISABLE_PROJECT_CONTEXT", Tier.ADVANCED, "Behavior",
        "Disable CLAUDE.md/AGENTS.md/CONTEXT.md injection.", default=False, parse=as_bool),
]


def _validate_schema() -> None:
    """Guard against duplicate / malformed names at import (cheap)."""
    seen: set[str] = set()
    for v in FIELDS:
        if not v.name.startswith("ADK_CC_"):
            raise ValueError(f"config schema: {v.name!r} must start with ADK_CC_")
        if v.name in seen:
            raise ValueError(f"config schema: duplicate var {v.name!r}")
        seen.add(v.name)


_validate_schema()

BY_NAME: dict[str, Var] = {v.name: v for v in FIELDS}


# --- cross-variable rules -------------------------------------------------
# Robustness: catch configurations that are individually valid but jointly
# wrong or dangerous — surfaced by `check` (and at boot) instead of failing
# silently or deep in a runtime call. Each rule is (resolved, is_set) -> msg|None.

@dataclass(frozen=True)
class Rule:
    level: str                                        # "error" | "warn"
    check: "Callable[[dict, dict], Optional[str]]"    # (resolved, is_set) -> message or None


def _truthy(resolved: dict, is_set: dict, name: str) -> bool:
    """A boolean var that is BOTH set and resolves truthy."""
    return bool(is_set.get(name)) and bool(resolved.get(name))


RULES: list[Rule] = [
    # Compaction requires both threshold + retention, or neither (ADK's
    # EventsCompactionConfig validator raises at boot otherwise).
    Rule("error", lambda r, s: (
        "ADK_CC_COMPACTION_TOKEN_THRESHOLD and ADK_CC_COMPACTION_EVENT_RETENTION "
        "must be set together (or neither)."
        if s.get("ADK_CC_COMPACTION_TOKEN_THRESHOLD") != s.get("ADK_CC_COMPACTION_EVENT_RETENTION")
        else None)),
    # External IdP without iss/aud verification is a silent security gap.
    Rule("warn", lambda r, s: (
        "ADK_CC_JWT_JWKS_URL is set but JWT_ISSUER/JWT_AUDIENCE are unset — "
        "the token issuer/audience are NOT verified."
        if s.get("ADK_CC_JWT_JWKS_URL") and not (s.get("ADK_CC_JWT_ISSUER") and s.get("ADK_CC_JWT_AUDIENCE"))
        else None)),
    # Two auth modes selected → only the higher-priority one runs.
    Rule("warn", lambda r, s: (
        "ADK_CC_AUTH_PASSWORD=1 and ADK_CC_JWT_JWKS_URL are both set — JWKS wins; "
        "password auth is ignored."
        if _truthy(r, s, "ADK_CC_AUTH_PASSWORD") and s.get("ADK_CC_JWT_JWKS_URL")
        else None)),
    # Allowlisted hosts do nothing unless the fetch mode is 'allowlist'.
    Rule("warn", lambda r, s: (
        "ADK_CC_WEB_FETCH_HOSTS is set but ADK_CC_WEB_FETCH_MODE is not 'allowlist' — "
        "the extra hosts are ignored."
        if s.get("ADK_CC_WEB_FETCH_HOSTS") and r.get("ADK_CC_WEB_FETCH_MODE") != "allowlist"
        else None)),
    # Security-relevant opt-outs — always worth surfacing.
    Rule("warn", lambda r, s: (
        "ADK_CC_WEB_FETCH_ALLOW_PRIVATE=1 — the SSRF guard is disabled "
        "(localhost/private IPs are fetchable)."
        if _truthy(r, s, "ADK_CC_WEB_FETCH_ALLOW_PRIVATE") else None)),
    Rule("warn", lambda r, s: (
        "ADK_CC_ALLOW_NO_AUTH=1 in a non-desktop deployment — the server runs with NO auth."
        if _truthy(r, s, "ADK_CC_ALLOW_NO_AUTH") and not r.get("ADK_CC_DESKTOP") else None)),
]


# --- public API -----------------------------------------------------------

def resolve(environ: Optional[dict] = None) -> dict[str, Any]:
    """Resolve every schema var against `environ` (default os.environ) → a
    name→value dict. Pure when passed an explicit dict."""
    env = os.environ if environ is None else environ
    return {v.name: v.resolve(env) for v in FIELDS}


def check(environ: Optional[dict] = None) -> tuple[list[str], list[str]]:
    """Validate an environment. Returns (errors, warnings).

    Errors (a boot-time misconfiguration): a REQUIRED var missing, a set value
    that isn't an allowed choice, or a cross-var invariant violated. Warnings: a
    conditionally-required var missing, a value that fails to parse, or a rule
    that flags a dangerous/ineffective-but-valid combination.
    """
    env = os.environ if environ is None else environ
    resolved = resolve(env)
    is_set: dict = {}
    errors: list[str] = []
    warnings: list[str] = []
    for v in FIELDS:
        set_raw = env.get(v.name)
        setp = set_raw is not None and set_raw.strip() != ""
        is_set[v.name] = setp
        # parse failure on a set value
        if setp and v.parse is not as_str:
            try:
                v.parse(set_raw)
            except Exception:
                warnings.append(f"{v.name}: value {set_raw!r} failed to parse; using default")
        # enum constraint
        if setp and v.choices and resolved[v.name] not in v.choices:
            errors.append(
                f"{v.name}: {set_raw!r} is not one of {'|'.join(map(str, v.choices))}"
            )
        # required (unconditional → error; conditional → warn)
        if v.tier is Tier.REQUIRED:
            required = True if v.required_if is None else bool(v.required_if(resolved))
            if required and not setp:
                cond = "" if v.required_if is None else " (conditionally required by current config)"
                (warnings if v.required_if is not None else errors).append(
                    f"{v.name}: required but not set{cond} — {v.help}"
                )
    # cross-var rules
    for rule in RULES:
        msg = rule.check(resolved, is_set)
        if msg:
            (errors if rule.level == "error" else warnings).append(msg)
    return errors, warnings


def _mask(value: Any) -> str:
    return "••••" if value not in (None, "") else "unset"


def render_effective(environ: Optional[dict] = None, show_secrets: bool = False) -> str:
    """Human-readable dump of effective values (secrets masked unless asked)."""
    resolved = resolve(environ)
    lines = ["# Effective adk-cc config (resolved from environment)"]
    for v in FIELDS:
        val = resolved[v.name]
        shown = (str(val) if (show_secrets or not v.secret) else _mask(val))
        lines.append(f"{v.name} = {shown}")
    return "\n".join(lines)


def render_env_example(profile: Profile = Profile.ALL) -> str:
    """Generate a tiered `.env.example`: a Quickstart (required + common) then a
    Reference (advanced + dev), grouped by section. `profile` filters
    profile-scoped vars (ALL renders everything, annotating web/desktop-only)."""

    def in_profile(v: Var) -> bool:
        if profile is Profile.ALL:
            return True
        return v.profile in (Profile.ALL, profile)

    def line(v: Var) -> list[str]:
        hint = f"  (one of: {'|'.join(map(str, v.choices))})" if v.choices else ""
        out = [f"# {v.help}{hint}"]
        tag = "" if v.profile is Profile.ALL else f"  ({v.profile.value}-only)"
        if v.tier is Tier.REQUIRED and v.required_if is None:
            out.append(f"{v.name}={v.example or ''}{tag}")
        else:
            shown = v.example if (v.tier is Tier.REQUIRED and v.example) else v.shown_default()
            out.append(f"# {v.name}={shown}{tag}")
        return out

    def section_block(title: str, vars_: list[Var]) -> list[str]:
        if not vars_:
            return []
        block = ["", f"# --- {title} " + "-" * max(0, 66 - len(title))]
        by_section: dict[str, list[Var]] = {}
        for v in vars_:
            by_section.setdefault(v.section, []).append(v)
        for section, sv in by_section.items():
            block.append(f"#\n# [{section}]")
            for v in sv:
                block.extend(line(v))
        return block

    visible = [v for v in FIELDS if in_profile(v)]
    quickstart = [v for v in visible if v.tier in (Tier.REQUIRED, Tier.COMMON)]
    reference = [v for v in visible if v.tier in (Tier.ADVANCED, Tier.DEV)]

    header = [
        "# adk-cc environment reference — GENERATED from agents/adk_cc/config/schema.py.",
        "# Do not edit by hand; run `python -m adk_cc.config gen-env`. Copy to `.env`.",
        f"# Profile: {profile.value}. Required vars are uncommented; everything else shows its default.",
    ]
    body = ["", "# ============================================================",
            "# QUICKSTART — required + commonly set", "# ============================================================"]
    body += section_block("Quickstart", quickstart)
    body += ["", "", "# ============================================================",
             "# REFERENCE — advanced tuning (sane defaults; rarely changed)",
             "# ============================================================"]
    body += section_block("Reference", reference)
    return "\n".join(header + body) + "\n"


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m adk_cc.config", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the current environment")
    pp = sub.add_parser("print", help="print effective values")
    pp.add_argument("--show-secrets", action="store_true")
    pg = sub.add_parser("gen-env", help="generate a tiered .env.example")
    pg.add_argument("--profile", choices=[x.value for x in Profile], default="all")
    pg.add_argument("--out", help="write to this path instead of stdout")
    args = p.parse_args(argv)

    if args.cmd == "check":
        errors, warnings = check()
        for w in warnings:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
        if not errors and not warnings:
            print("OK — environment satisfies the schema.")
        return 1 if errors else 0
    if args.cmd == "print":
        print(render_effective(show_secrets=args.show_secrets))
        return 0
    if args.cmd == "gen-env":
        text = render_env_example(Profile(args.profile))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {args.out} ({text.count(chr(10))} lines)")
        else:
            print(text, end="")
        return 0
    return 2
