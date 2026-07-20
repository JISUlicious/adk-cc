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
        default="bypassPermissions"),
    Var("ADK_CC_PERMISSIONS_YAML", Tier.COMMON, "Permissions",
        "Path to a YAML of permission rules + authz policies.",
        default=None, parse=as_path),

    # --- Sandbox (selector + core; per-backend groups below) -------------
    Var("ADK_CC_SANDBOX_BACKEND", Tier.COMMON, "Sandbox",
        "noop | container | docker | e2b | sandbox_service | daytona | ssh. Default: noop (host exec).",
        default="noop"),
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
    Var("ADK_CC_SESSION_DSN", Tier.COMMON, "Deployment",
        "Persistent session store DSN (postgres/sqlite). Unset = in-memory. Ignored in desktop.",
        default=None, profile=Profile.WEB, default_display="in-memory"),
    Var("ADK_CC_ARTIFACT_STORAGE_URI", Tier.COMMON, "Deployment",
        "Artifact persistence URI (file://, gs://, s3://). Unset = in-memory.",
        default=None, default_display="in-memory"),
    Var("ADK_CC_DESKTOP_DATA", Tier.ADVANCED, "Deployment",
        "Desktop data root (sessions, settings, secrets, checkpoints).",
        default=None, parse=as_path, default_display="~/.adk-cc-desktop", profile=Profile.DESKTOP),
    Var("ADK_CC_AGENTS_DIR", Tier.REQUIRED, "Deployment",
        "Path to the agents/ package dir (web factory currently requires it; default fix pending).",
        default=None, parse=as_path,
        required_if=lambda c: not c.get("ADK_CC_DESKTOP")),
    Var("ADK_CC_SERVE_UI", Tier.ADVANCED, "Deployment",
        "Mount the SPA (set by the desktop app / UI deployments).",
        default=False, parse=as_bool),
    Var("ADK_CC_LOG_LEVEL", Tier.COMMON, "Deployment",
        "Log verbosity (DEBUG/INFO/WARNING/…).", default="INFO"),
    Var("ADK_CC_LOG_FORMAT", Tier.COMMON, "Deployment",
        "Log format: text | json.", default="text"),

    # --- Misc feature flags ----------------------------------------------
    Var("ADK_CC_TOLERANT_TOOL_JSON", Tier.ADVANCED, "Behavior",
        "Default-on repair of malformed tool-call JSON (set 0 to disable).",
        default=True, parse=as_bool),
    Var("ADK_CC_CHECKPOINT", Tier.ADVANCED, "Behavior",
        "Shadow-git checkpoint/undo (default-on in desktop; set 0 to disable).",
        default=True, parse=as_bool),
    Var("ADK_CC_WEB_FETCH_MODE", Tier.ADVANCED, "Behavior",
        "web_fetch host policy: open (SSRF-guarded) | allowlist.", default="open"),
    Var("ADK_CC_SKIP_DOTENV", Tier.DEV, "Behavior",
        "Skip .env loading (CI/containers where env is already populated).",
        default=False, parse=as_bool),
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


# --- public API -----------------------------------------------------------

def resolve(environ: Optional[dict] = None) -> dict[str, Any]:
    """Resolve every schema var against `environ` (default os.environ) → a
    name→value dict. Pure when passed an explicit dict."""
    env = os.environ if environ is None else environ
    return {v.name: v.resolve(env) for v in FIELDS}


def check(environ: Optional[dict] = None) -> tuple[list[str], list[str]]:
    """Validate an environment. Returns (errors, warnings).

    - error: an unconditionally-REQUIRED var is missing.
    - warning: a conditionally-required var is missing given the resolved config,
      or a set var fails to parse.
    """
    env = os.environ if environ is None else environ
    resolved = resolve(env)
    errors: list[str] = []
    warnings: list[str] = []
    for v in FIELDS:
        set_raw = env.get(v.name)
        is_set = set_raw is not None and set_raw.strip() != ""
        # parse failure on a set value
        if is_set and v.parse is not as_str:
            try:
                v.parse(set_raw)
            except Exception:
                warnings.append(f"{v.name}: value {set_raw!r} failed to parse; using default")
        if v.tier is not Tier.REQUIRED:
            continue
        required = True if v.required_if is None else bool(v.required_if(resolved))
        if required and not is_set:
            cond = "" if v.required_if is None else " (conditionally required by current config)"
            (warnings if v.required_if is not None else errors).append(
                f"{v.name}: required but not set{cond} — {v.help}"
            )
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
        out = [f"# {v.help}"]
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
