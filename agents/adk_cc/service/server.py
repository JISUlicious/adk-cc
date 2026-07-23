"""FastAPI server factory.

Wraps ADK's `get_fast_api_app` with adk-cc's optional auth middleware
and SPA mount. Operators run this via uvicorn:

    uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000

The plugin stack ([Audit, Tenancy, Permission, Quota, PlanModeReminder,
TaskReminder, ToolCallValidator, ContextGuard, ProjectContext,
AskUserQuestionUiHint, ConfirmationFormUi, ModelIOTrace]) is declared in
`adk_cc/agent.py::App.plugins` and picked up via ADK's App-discovery
path — we don't pass instances through `extra_plugins` because that
parameter expects import-path strings in ADK 1.31.1. Plugin
configuration is env-driven and applied at agent.py module load:

    ADK_CC_PERMISSION_MODE      → PermissionPlugin default mode
    ADK_CC_PERMISSIONS_YAML     → PermissionPlugin rule hierarchy
    ADK_CC_WORKSPACE_ROOT       → TenancyPlugin default workspace root
    ADK_CC_QUOTA_PER_MINUTE     → QuotaPlugin per-tenant rate cap
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from ..config.schema import env_bool


def build_fastapi_app(
    *,
    agents_dir: str,
    session_service_uri: Optional[str] = None,
    auth_extractor=None,
    identity=None,
    serve_web: bool = False,
    ui_dist_dir: Optional[str] = None,
):
    """Build a production FastAPI app.

    Args:
      agents_dir: AGENTS_DIR for ADK's loader (parent of `adk_cc/`).
      session_service_uri: e.g. "postgresql://...", "sqlite:///./adk-cc.db".
        None → in-memory (dev only).
      auth_extractor: callable(request) → (user_id, tenant_id); None → no auth.
      serve_web: True to also mount ADK's bundled web UI; False = API only.
      ui_dist_dir: path to the adk-cc custom UI build artifacts (Vite
        `web/dist/`). When set and the directory exists, StaticFiles
        is mounted at `/` so the same FastAPI process serves both API
        and UI. Mutually exclusive with `serve_web=True` (the bundled
        ADK UI also claims `/`).

    The plugin chain (Audit, Tenancy, Permission, Quota, PlanModeReminder,
    TaskReminder, ToolCallValidator, ContextGuard, ProjectContext,
    AskUserQuestionUiHint, ConfirmationFormUi, ModelIOTrace) is declared
    in `adk_cc/agent.py::App.plugins` and picked up automatically by
    ADK's App discovery — we do NOT pass plugin instances through
    `extra_plugins`, because in ADK 1.31.1 that parameter expects a
    list of import-path strings ("module:Class") and silently fails
    on instances. Configuration is env-driven and applied at agent.py
    module load (ADK_CC_PERMISSIONS_YAML, ADK_CC_PERMISSION_MODE,
    ADK_CC_WORKSPACE_ROOT, ADK_CC_QUOTA_PER_MINUTE).
    """
    from google.adk.cli.fast_api import get_fast_api_app

    # Artifact storage: in-memory default (fine for dev), env-driven
    # override for persistence. ADK accepts URIs like
    # `gs://bucket/prefix` for GCS; a local path string also works as
    # of 1.31.1. The `save_as_artifact` tool relies on this service.
    artifact_uri = os.environ.get("ADK_CC_ARTIFACT_STORAGE_URI") or None

    # adk-cc adds an `s3://` artifact scheme (AWS S3 + S3-compatible
    # stores: MinIO / R2 / Wasabi / B2 / Ceph) on top of ADK's built-in
    # memory:// / file:// / gs://. Register it before get_fast_api_app
    # resolves the URI. Connection details (endpoint, region, creds) come
    # from the environment — see register_s3_artifact_scheme().
    if artifact_uri and artifact_uri.startswith("s3://"):
        from ..artifacts import register_s3_artifact_scheme

        register_s3_artifact_scheme()

    # Server lifespan. ALWAYS present: at startup it warms the LiteLlm delegate
    # off the loop so the first request doesn't pay litellm's cold import on the
    # event loop. When ADK_CC_MEMORY=1 and ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S>0
    # it ALSO runs the periodic episodic→semantic consolidation pass for the
    # server's lifetime. ADK wraps the passed lifespan in its own (async with
    # lifespan), so startup/shutdown both fire.
    from .memory_scheduler import make_consolidation_lifespan

    # Desktop file-based session store: register the `adkccfiles://` scheme so a
    # session_service_uri of that scheme resolves (via ADK's service registry) to
    # per-project JSONL files instead of sqlite. Cheap + idempotent; the scheme is
    # only USED when the resolved DSN selects it (desktop mode), so this is inert
    # for web deployments.
    from .file_session_service import register_file_session_scheme
    register_file_session_scheme()

    fastapi_app = get_fast_api_app(
        agents_dir=agents_dir,
        session_service_uri=session_service_uri,
        artifact_service_uri=artifact_uri,
        web=serve_web,
        lifespan=make_consolidation_lifespan(),
    )

    # Durable runs (Turn Broker — analysis/durable-runs-design.md): turns
    # execute as server-side tasks; /api/turns/* is a re-attachable tail, so a
    # client disconnect no longer kills the run. Uses the SAME runner+session
    # service as ADK's routes (extracted from the AdkWebServer the app was
    # built around). Degrades gracefully if ADK's internals shift: the legacy
    # /run_sse path is untouched either way.
    from .turn_routes import mount_turn_routes
    from .turns import TurnBroker, extract_adk_web_server

    _adk_server = extract_adk_web_server(fastapi_app)
    if _adk_server is not None:
        mount_turn_routes(
            fastapi_app,
            TurnBroker(
                get_runner=_adk_server.get_runner_async,
                session_service=_adk_server.session_service,
            ),
        )
    else:  # pragma: no cover — ADK internals changed
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "durable turns disabled: AdkWebServer not found behind "
            "get_fast_api_app (ADK internals changed?) — /run_sse still works."
        )

    # Context-fullness limits for the UI gauge (compaction-indicator P2). Returns
    # the resolved ladder (max/reserve/effective/warn/reject + compaction
    # threshold), or {} when the guard is disabled. Read-only; behind auth.
    @fastapi_app.get("/api/context/limits", include_in_schema=False)
    def _context_limits():  # noqa: ANN202
        from ..plugins.context_guard import resolved_limits
        return resolved_limits() or {}

    # Knowledge-graph endpoints (opt-in, ADK_CC_KNOWLEDGE_UI=1). No-op otherwise.
    from .graph_routes import mount_knowledge_routes
    mount_knowledge_routes(fastapi_app)

    if auth_extractor is not None:
        from .auth import make_auth_middleware

        # When the SPA bundle is mounted, exempt its public paths from
        # auth — the React app *itself* is what asks the user to sign in,
        # so the HTML + JS + CSS must load anonymously. API routes
        # (/run, /run_sse, /list-apps, /apps/*, /debug/*) stay gated.
        #
        # The admin SPA PAGE routes (the shell HTML) must load anonymously so
        # the React app can boot and THEN make authenticated, admin-gated API
        # calls. These are EXACT page paths only — we deliberately do NOT
        # exempt the `/admin/` prefix, because the admin API lives under
        # `/admin/model-endpoints` and must stay behind auth + the admin-role
        # gate. (The tenant admin API at `/tenants/*` is likewise gated.)
        if ui_dist_dir:
            # SPA PAGE shells must load anonymously (the React app boots, then
            # makes authenticated API calls). API routes stay gated.
            spa_pages = (
                "/admin", "/admin/users", "/admin/usage", "/admin/audit",
                "/admin/mcp", "/admin/skills", "/admin/models",
                "/knowledge", "/org", "/account",
            )
            exempt_exact = ("/", "/favicon.svg", "/favicon.ico") + spa_pages
            exempt_prefixes = ("/assets/",)
        else:
            exempt_exact = ()
            exempt_prefixes = ()
        # The in-house identity endpoints that MUST be reachable without a
        # token (login/signup/config + the JWKS the SPA & external verifiers
        # read). /auth/me stays gated.
        if identity is not None:
            from .identity_routes import PUBLIC_PATHS
            from .identity_routes import PUBLIC_PREFIXES as RESET_PREFIXES
            from .org_routes import PUBLIC_PREFIXES as INVITE_PREFIXES

            exempt_exact = exempt_exact + PUBLIC_PATHS
            # /auth/invite/* + /auth/reset/* (the public token APIs) and the
            # /invite/<token> + /reset-password/<token> SPA page shells, so
            # the link holder can land + act before signing in.
            exempt_prefixes = (exempt_prefixes + INVITE_PREFIXES + RESET_PREFIXES
                               + ("/invite/", "/reset-password/"))
        # REST authZ gate (closes trust-the-path). Added BEFORE the auth
        # middleware so it ends up INNER: Starlette runs the last-added
        # middleware outermost, so auth (added next) runs first and sets
        # request.state.adk_cc_auth, then this inner gate reads it. No-op
        # unless ADK_CC_AUTHZ=1 (checked per-request inside the middleware).
        from .authz_routes import make_authz_middleware
        from ..plugins.authz import _default_pdp

        fastapi_app.add_middleware(make_authz_middleware(_default_pdp()))

        fastapi_app.add_middleware(
            make_auth_middleware(
                auth_extractor,
                exempt_path_prefixes=exempt_prefixes,
                exempt_exact_paths=exempt_exact,
            )
        )

    # In-house email+password identity routes (/auth/*, JWKS). Mounted only
    # when an IdentityService is configured (ADK_CC_AUTH_PASSWORD=1), before
    # the UI catch-all so the routes win on path match.
    if identity is not None:
        from .identity_routes import mount_identity_routes
        from .org_routes import mount_org_routes

        # Per-user secrets (Settings → Secrets) need a CredentialProvider; built
        # from the same env knobs as the agent/MCP/sandbox so all observe one
        # store. None → the /auth/secrets routes simply aren't mounted.
        from ..credentials import credential_provider_from_env

        mount_identity_routes(
            fastapi_app, identity, credentials=credential_provider_from_env()
        )
        mount_org_routes(fastapi_app, identity)

    # Desktop project registry (default-OFF; ADK_CC_DESKTOP=1). Mounted before
    # the UI catch-all so /desktop/* wins on path match.
    from .desktop_routes import desktop_enabled, mount_desktop_routes
    # First-run: drop a settings.env template in the data dir so the user has a
    # place to put their model API key/endpoint (loaded by the dotenv bootstrap
    # on the next launch). No-op if it already exists.
    if desktop_enabled():
        from .desktop_config import ensure_settings_template
        ensure_settings_template()
        # Reap any container-sandbox containers left by a previous (crashed /
        # force-killed) run of THIS single-instance desktop app, before any
        # session is live. Best-effort; no-op without a runtime. See
        # sweep_orphans' contract (startup-only, single-instance).
        try:
            import logging as _logging

            from .. import deployment as _dep

            if _dep.sandbox_mode() == "container":
                from ..sandbox.backends.local_container_backend import sweep_orphans

                n = sweep_orphans()
                if n:
                    _logging.getLogger(__name__).info(
                        "sandbox: reaped %d orphan container(s) at startup", n)
        except Exception:  # noqa: BLE001 — never block startup
            pass
    mount_desktop_routes(fastapi_app)
    # Desktop layered settings (global + per-project MCP/skills/secrets, global
    # models) over the same stores the agent reads — no-auth, scope-keyed.
    from .desktop_settings import mount_desktop_settings_routes
    mount_desktop_settings_routes(fastapi_app)
    # Desktop read-only file tree + file viewer over the session's in-place
    # workspace (the project root; for the right-side file panel). Scoped +
    # path-guarded; no-auth.
    from .desktop_files import mount_desktop_files_routes
    mount_desktop_files_routes(fastapi_app)

    # Admin panel routes (default-OFF). Mounted BEFORE the UI StaticFiles
    # mount — the SPA is mounted at `/` (a catch-all) and would otherwise
    # shadow the admin API routes. Built against the same registry /
    # credential store / skills dir the agent reads, so edits hot-reload.
    _mount_admin_if_enabled(fastapi_app)

    if ui_dist_dir:
        _mount_ui(fastapi_app, ui_dist_dir)

    return fastapi_app


def _mount_ui(app, dist_dir: str) -> None:
    """Mount the Vite-built SPA at `/`.

    Mounted last so ADK's API routes (`/run`, `/run_sse`, `/list-apps`,
    `/apps/*`, `/debug/*`) win on path match. StaticFiles only catches
    leftover paths (`/`, `/assets/*`, `/favicon.svg`).

    SPA route fallback: for unmatched sub-paths (e.g. react-router
    `/chat/abc` once we add routes in Phase 2+), serve index.html. We
    install a small catch-all that delegates to the bundle's
    `index.html` so client-side routing works on hard refresh.

    The auth middleware (when present) runs before StaticFiles, but it
    only enforces auth on API paths — the middleware exempts the SPA
    assets so the login form can load.
    """
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    dist = Path(dist_dir)
    if not dist.is_dir():
        # Fail loud — operators who opt in expect static assets to exist.
        raise RuntimeError(
            f"ui_dist_dir={dist_dir!r} does not exist or is not a directory. "
            f"Run `npm --prefix web run build` before starting the server."
        )

    index_html = dist / "index.html"
    if not index_html.is_file():
        raise RuntimeError(
            f"{index_html} not found. Did the Vite build complete?"
        )

    # SPA fallback. Registered before the StaticFiles mount so `/` and the
    # known client-side routes return index.html on hard refresh / deep link
    # (react-router then renders the right page). We enumerate the SPA route
    # prefixes explicitly rather than a blanket catch-all, so genuinely
    # missing API routes still surface as 404 instead of silently returning
    # the SPA shell.
    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(index_html)

    # Client-side routes (react-router). Enumerated EXACTLY — NOT a
    # `/admin/{path}` catch-all, which would shadow the admin API routes
    # (e.g. /admin/model-endpoints) that get registered later. Add new SPA
    # page tabs here as they're created (and to the auth-exempt list above).
    for _spa_path in (
        "/admin", "/admin/users", "/admin/usage", "/admin/audit",
        "/admin/mcp", "/admin/skills", "/admin/models",
        "/knowledge", "/org", "/account",
    ):
        app.add_api_route(
            _spa_path,
            lambda: FileResponse(index_html),
            methods=["GET"],
            include_in_schema=False,
        )

    # Parameterized SPA routes: the public accept-invite page /invite/<token>
    # and the one-time password-reset page /reset-password/<token>.
    @app.get("/invite/{token}", include_in_schema=False)
    def _spa_invite(token: str) -> FileResponse:
        return FileResponse(index_html)

    @app.get("/reset-password/{token}", include_in_schema=False)
    def _spa_reset(token: str) -> FileResponse:
        return FileResponse(index_html)

    app.mount("/", StaticFiles(directory=str(dist), html=False), name="ui")


def make_app():
    """Default factory consumed by `uvicorn ... --factory`.

    Reads everything from env so the deployment is config-driven:
      ADK_CC_AGENTS_DIR        (optional; defaults to this package's agents/ dir)
      ADK_CC_SESSION_DSN       (optional; e.g. postgresql://...)
      ADK_CC_PERMISSIONS_YAML  (optional)
      ADK_CC_PERMISSION_MODE   (optional)
      ADK_CC_AUDIT_LOG         (optional)
      ADK_CC_QUOTA_PER_MINUTE  (optional)
      ADK_CC_WORKSPACE_ROOT    (optional)
      ADK_CC_JWT_JWKS_URL      (optional; selects JwtAuthExtractor)
      ADK_CC_JWT_ISSUER        (optional, used with JWKS_URL)
      ADK_CC_JWT_AUDIENCE      (optional, used with JWKS_URL)
      ADK_CC_JWT_USER_CLAIM    (optional, default "sub")
      ADK_CC_JWT_TENANT_CLAIM  (optional, default "tenant")
      ADK_CC_JWT_ROLES_CLAIM   (optional, default "roles"; feeds authZ subject)
      ADK_CC_JWT_SCOPES_CLAIM  (optional, default "scope"; feeds authZ subject)
      ADK_CC_AUTH_TOKENS       (optional fallback, see auth.BearerTokenExtractor)
      ADK_CC_AUTHZ             (optional; "1" enables the authZ gates, default off)
      ADK_CC_ALLOW_NO_AUTH     (optional dev escape — see below)
      ADK_CC_SERVE_UI          (optional; "1" to mount the custom UI bundle)
      ADK_CC_UI_DIST           (optional; dir to serve; default: <repo>/web/dist)
      ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S
                               (optional; with ADK_CC_MEMORY=1, run the periodic
                                episodic→semantic consolidation in-process every
                                N seconds instead of via the external cron.
                                Deterministic/no-model; single-worker only —
                                see service/memory_scheduler.py)
      ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD
                               (optional; with ADK_CC_MEMORY=1, promote a user
                                as soon as N unprocessed episodics stack up,
                                from the capture path. The responsive half of
                                the hybrid; pair with the interval above for the
                                time-based sweep — see plugins/memory.py)

    Auth extractor selection (first match wins):
      1. ADK_CC_JWT_JWKS_URL set → JwtAuthExtractor (production).
      2. ADK_CC_AUTH_TOKENS set → BearerTokenExtractor (dev).
      3. Otherwise fail-closed unless ADK_CC_ALLOW_NO_AUTH=1.

    Operators with bespoke auth (session DB, mTLS, custom IdP integration)
    should implement an `AuthExtractor` and call `build_fastapi_app(
    auth_extractor=...)` from their own factory rather than `make_app`.

    `.env` auto-load: matches `adk web`'s behavior. The bootstrap runs
    once at `adk_cc` package import (`adk_cc/__init__.py`), before the
    agent module is loaded. Disable via `ADK_CC_SKIP_DOTENV=1`. See the
    package `__init__.py` for the lookup order.
    """
    # NB: the config self-check runs at PACKAGE import (adk_cc/__init__.py
    # `_boot_config_check`), before this factory is even reachable — so it
    # covers `adk web`/`adk run` and the desktop sidecar too, and genuinely
    # precedes the agent-graph construction.
    agents_dir = os.environ.get("ADK_CC_AGENTS_DIR")
    if not agents_dir:
        # Default to this package's own agents/ dir — in a SOURCE checkout
        # server.py lives at agents/adk_cc/service/server.py, so parents[2] is
        # the agents/ root ADK discovers the agent package under. When the
        # package is pip-installed (site-packages/adk_cc/service/server.py),
        # parents[2] is site-packages itself — handing ADK's discovery every
        # installed distribution as a candidate agent — so in that layout the
        # default is WRONG and we keep the old loud failure instead.
        candidate = Path(__file__).resolve().parents[2]
        if candidate.name != "agents":
            raise RuntimeError(
                "ADK_CC_AGENTS_DIR must be set: adk_cc is installed as a "
                f"package (auto-detected root {candidate} is not an agents/ "
                "dir), so point ADK_CC_AGENTS_DIR at the directory that "
                "contains your agent packages."
            )
        agents_dir = str(candidate)

    # Admin panel (default-OFF). When enabled, default the tenant registry /
    # skills dirs in the environment BEFORE the agent module loads (it reads
    # them at import time, inside build_fastapi_app → get_fast_api_app), so
    # the agent's tenant MCP/skill toolsets resolve against the SAME store
    # the admin routes write to — that's what makes edits take effect live.
    # Must run before build_fastapi_app(). See _prepare_admin_env().
    _prepare_admin_env()

    extractor = None
    identity = None
    if os.environ.get("ADK_CC_JWT_JWKS_URL"):
        from .auth import JwtAuthExtractor

        extractor = JwtAuthExtractor(
            jwks_url=os.environ["ADK_CC_JWT_JWKS_URL"],
            issuer=os.environ.get("ADK_CC_JWT_ISSUER"),
            audience=os.environ.get("ADK_CC_JWT_AUDIENCE"),
            user_claim=os.environ.get("ADK_CC_JWT_USER_CLAIM", "sub"),
            tenant_claim=os.environ.get("ADK_CC_JWT_TENANT_CLAIM", "tenant"),
            roles_claim=os.environ.get("ADK_CC_JWT_ROLES_CLAIM", "roles"),
            scopes_claim=os.environ.get("ADK_CC_JWT_SCOPES_CLAIM", "scope"),
        )
    elif env_bool("ADK_CC_AUTH_PASSWORD"):
        # In-house email+password identity: adk-cc mints its own RS256 tokens
        # and validates them in-process via the same JwtAuthExtractor (no
        # network). The /auth/* routes are mounted by build_fastapi_app.
        from ..identity import IdentityService

        identity = IdentityService.build_from_env()
        extractor = identity.make_extractor()
    elif os.environ.get("ADK_CC_AUTH_TOKENS"):
        from .auth import BearerTokenExtractor

        extractor = BearerTokenExtractor()

    if extractor is None and not env_bool("ADK_CC_ALLOW_NO_AUTH"):
        raise RuntimeError(
            "make_app(): no auth extractor configured. Pick one:\n"
            "  - Set ADK_CC_JWT_JWKS_URL (and optionally ISSUER/AUDIENCE) "
            "for the production JwtAuthExtractor (external IdP / Keycloak).\n"
            "  - Set ADK_CC_AUTH_PASSWORD=1 for the built-in email+password "
            "identity provider (local accounts; mints + validates its own JWTs).\n"
            "  - Set ADK_CC_AUTH_TOKENS=token=user:tenant,... for the "
            "built-in dev BearerTokenExtractor.\n"
            "  - Implement an AuthExtractor and call "
            "build_fastapi_app(auth_extractor=...) from your own factory.\n"
            "  - For local dev only, set ADK_CC_ALLOW_NO_AUTH=1 to "
            "acknowledge the no-auth deployment."
        )

    ui_dist_dir: Optional[str] = None
    if env_bool("ADK_CC_SERVE_UI"):
        explicit = os.environ.get("ADK_CC_UI_DIST")
        if explicit:
            ui_dist_dir = explicit
        else:
            # Default: <repo_root>/web/dist. server.py lives at
            # agents/adk_cc/service/server.py, so repo_root is three
            # parents up (service → adk_cc → agents → repo).
            ui_dist_dir = str(Path(__file__).resolve().parents[3] / "web" / "dist")

    # build_fastapi_app mounts the admin panel (when enabled) before the UI.
    return build_fastapi_app(
        agents_dir=agents_dir,
        session_service_uri=_resolve_session_dsn(),
        auth_extractor=extractor,
        identity=identity,
        ui_dist_dir=ui_dist_dir,
    )


def _resolve_session_dsn() -> Optional[str]:
    """The session-service URI. Desktop mode uses the per-project JSONL file store
    (`adkccfiles://<data-dir>`, see file_session_service) rather than the
    monolithic sqlite — the profile carries the policy, so the sqlite DSN main.rs
    still sets in ADK_CC_SESSION_DSN is intentionally overridden. Web mode uses
    ADK_CC_SESSION_DSN as-is (postgres/sqlite/…)."""
    from .. import deployment

    if deployment.is_desktop():
        return f"adkccfiles://{deployment.desktop_data_dir()}"
    return os.environ.get("ADK_CC_SESSION_DSN")


# --- Admin panel wiring ---------------------------------------------------

# Global-mode defaults: the admin panel manages ONE deployment-wide config
# set. It rides the per-tenant registry machinery (which hot-reloads per
# invocation) pinned to a single tenant id — matching what TenancyPlugin's
# default resolver produces for an unauthenticated / single-tenant run.
#
# LEGACY location: before the ADK_CC_DATA_DIR root existed, admin data
# defaulted to this cwd-relative path. _prepare_admin_env still prefers it
# when it exists and the new home-rooted location doesn't, so upgrading
# doesn't orphan an existing admin store.
_ADMIN_LEGACY_DATA_DIR = ".adk-cc/admin-data"


def _admin_enabled() -> bool:
    return env_bool("ADK_CC_ADMIN_PANEL")


def _global_tenant_id() -> str:
    return os.environ.get("ADK_CC_GLOBAL_TENANT_ID", "local")


def _prepare_admin_env() -> None:
    """Default the tenant registry/skills dirs in os.environ when the admin
    panel is on, so the agent module (loaded next, inside build_fastapi_app)
    wires its tenant MCP/skill toolsets against the admin-managed store.

    No-op if the admin panel is off, or if the operator already set the
    tenant env vars (those win). Idempotent.
    """
    if not _admin_enabled():
        return
    from .. import deployment as _dep

    base = os.environ.get("ADK_CC_ADMIN_DATA_DIR")
    if not base:
        new_default = (_dep.data_dir() / "admin-data").resolve()
        legacy = Path(_ADMIN_LEGACY_DATA_DIR).resolve()
        if legacy.is_dir() and not new_default.exists():
            # Existing pre-DATA_DIR admin store in the old cwd-relative spot —
            # keep using it rather than silently starting an empty one.
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "admin data found at legacy %s — using it; move it to %s (or set "
                "ADK_CC_ADMIN_DATA_DIR) to silence this.", legacy, new_default,
            )
            base = str(legacy)
        else:
            base = str(new_default)
    os.environ["ADK_CC_ADMIN_DATA_DIR"] = base
    os.environ.setdefault("ADK_CC_TENANT_REGISTRY_DIR", str(Path(base) / "registry"))
    os.environ.setdefault("ADK_CC_TENANT_SKILLS_DIR", str(Path(base) / "skills"))
    # Model-endpoint registry file — the agent reads this at import to wrap
    # MODEL in a SelectableLlm (live model switching). Same default-dir base.
    os.environ.setdefault(
        "ADK_CC_MODEL_REGISTRY_FILE", str(Path(base) / "model-endpoints.json")
    )
    # Dev default for the credential store: in-memory (lost on restart).
    # Operators set ADK_CC_CREDENTIAL_PROVIDER=encrypted_file (+ KEY +
    # STORE_DIR) for persistence — same vars the tenant path already uses.
    os.environ.setdefault("ADK_CC_CREDENTIAL_PROVIDER", "memory")
    # Seed the boot model (from env) as endpoint #1 so it appears in the
    # panel and stays the default until an operator activates another. The
    # agent's SelectableLlm resolves this file lazily per request.
    _seed_model_registry()


def _seed_model_registry() -> None:
    """Seed the model-endpoint registry with the boot model (idempotent)."""
    from ..models import ModelEndpointConfig, ModelEndpointRegistry

    path = os.environ.get("ADK_CC_MODEL_REGISTRY_FILE")
    if not path:
        return
    ModelEndpointRegistry(path).seed_default(
        ModelEndpointConfig(
            name="default",
            model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
            api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
            api_key_env="ADK_CC_API_KEY",
        )
    )


def _build_credential_provider():
    """CredentialProvider for the tenant-admin panel.

    Delegates to the ONE canonical factory so provider selection, store-dir
    defaulting, and error messages are identical everywhere (previously this
    re-implemented the selection and diverged: no `none` handling, and it
    required CREDENTIAL_STORE_DIR instead of defaulting it under DATA_DIR).
    The admin panel manages secrets, so `PROVIDER=none` (no store) is rejected
    here with a specific reason rather than mounting a panel that can't persist
    anything — or, as before, dying with a misleading 'unknown provider'."""
    from ..credentials import credential_provider_from_env

    provider = credential_provider_from_env()
    if provider is None:  # PROVIDER=none
        raise RuntimeError(
            "ADK_CC_CREDENTIAL_PROVIDER=none is incompatible with the tenant "
            "admin panel, which needs a credential store — use memory or "
            "encrypted_file."
        )
    return provider


def _make_admin_role_extractor():
    """Build the admin authorization hook: require the admin role on the
    authenticated principal (configurable name via ADK_CC_ADMIN_ROLE,
    default 'admin'), and, in global mode, that the target tenant is the
    global tenant. Raises HTTPException(401/403) on denial."""
    from fastapi import HTTPException

    required_role = os.environ.get("ADK_CC_ADMIN_ROLE", "admin")

    def authorize(request, target: str) -> None:
        auth = getattr(request.state, "adk_cc_auth", None)
        if auth is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        roles = getattr(auth, "roles", frozenset()) or frozenset()
        if required_role not in roles:
            raise HTTPException(
                status_code=403, detail=f"admin role {required_role!r} required"
            )
        # Tenant-scoped routes pass a tenant id as `target`: an admin may only
        # manage their OWN tenant's resources (org_id == tenant_id). Global
        # routes (e.g. model endpoints) pass a non-tenant scope string in
        # `_GLOBAL_SCOPES`, exempt from the tenant check (role-gated above).
        caller_tenant = getattr(auth, "tenant_id", None)
        if target not in _GLOBAL_SCOPES and target != caller_tenant:
            raise HTTPException(
                status_code=403,
                detail="you can only manage your own org's resources",
            )

    return authorize


# Non-tenant admin scope strings passed to the authorize hook by global
# (not tenant-scoped) admin routes — exempt from the global-tenant match.
_GLOBAL_SCOPES = frozenset({"model-endpoints"})


def _mount_admin_if_enabled(app) -> None:
    """Mount the tenant-admin routes for the global tenant when the admin
    panel is enabled. No-op otherwise (default)."""
    if not _admin_enabled():
        return
    # Idempotent — ensures the registry/skills/model env vars are defaulted
    # even when build_fastapi_app is called directly (not via make_app).
    _prepare_admin_env()
    from ..models import ModelEndpointRegistry
    from ..service.registry import JsonFileTenantResourceRegistry
    from ..tools.mcp_tenant import McpServerConfig
    from .admin_routes import mount_model_admin, mount_tenant_admin

    authorize = _make_admin_role_extractor()

    registry_dir = os.environ["ADK_CC_TENANT_REGISTRY_DIR"]  # set by _prepare_admin_env
    registry = JsonFileTenantResourceRegistry[McpServerConfig](
        root=registry_dir,
        kind="mcp",
        model=McpServerConfig,
        id_attr="server_name",
    )
    mount_tenant_admin(
        app,
        registry=registry,
        credentials=_build_credential_provider(),
        skill_root=os.environ.get("ADK_CC_TENANT_SKILLS_DIR"),
        admin_extractor=authorize,
    )
    # Model-endpoint routes share the same admin gate + the registry file the
    # agent's SelectableLlm reads, so an activate here switches the live model.
    mount_model_admin(
        app,
        registry=ModelEndpointRegistry(os.environ["ADK_CC_MODEL_REGISTRY_FILE"]),
        authorize=authorize,
    )
