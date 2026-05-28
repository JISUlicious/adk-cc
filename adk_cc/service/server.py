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


def build_fastapi_app(
    *,
    agents_dir: str,
    session_service_uri: Optional[str] = None,
    auth_extractor=None,
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

    fastapi_app = get_fast_api_app(
        agents_dir=agents_dir,
        session_service_uri=session_service_uri,
        web=serve_web,
    )

    if auth_extractor is not None:
        from .auth import make_auth_middleware

        # When the SPA bundle is mounted, exempt its public paths from
        # auth — the React app *itself* is what asks the user to sign in,
        # so the HTML + JS + CSS must load anonymously. API routes
        # (/run, /run_sse, /list-apps, /apps/*, /debug/*) stay gated.
        if ui_dist_dir:
            exempt_exact = ("/", "/favicon.svg", "/favicon.ico")
            exempt_prefixes = ("/assets/",)
        else:
            exempt_exact = ()
            exempt_prefixes = ()
        fastapi_app.add_middleware(
            make_auth_middleware(
                auth_extractor,
                exempt_path_prefixes=exempt_prefixes,
                exempt_exact_paths=exempt_exact,
            )
        )

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

    # Catch-all SPA fallback. Registered before the StaticFiles mount so
    # `/` and arbitrary subpaths return index.html when they don't match
    # an API route or a real static asset. We register only specific
    # subpath prefixes to avoid swallowing 404s for legitimately missing
    # API routes (which should still surface as 404, not as the SPA).
    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(index_html)

    app.mount("/", StaticFiles(directory=str(dist), html=False), name="ui")


def make_app():
    """Default factory consumed by `uvicorn ... --factory`.

    Reads everything from env so the deployment is config-driven:
      ADK_CC_AGENTS_DIR        (required)
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
      ADK_CC_AUTH_TOKENS       (optional fallback, see auth.BearerTokenExtractor)
      ADK_CC_ALLOW_NO_AUTH     (optional dev escape — see below)
      ADK_CC_SERVE_UI          (optional; "1" to mount the custom UI bundle)
      ADK_CC_UI_DIST           (optional; dir to serve; default: <repo>/web/dist)

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
    agents_dir = os.environ.get("ADK_CC_AGENTS_DIR")
    if not agents_dir:
        raise RuntimeError("ADK_CC_AGENTS_DIR must be set for make_app()")

    extractor = None
    if os.environ.get("ADK_CC_JWT_JWKS_URL"):
        from .auth import JwtAuthExtractor

        extractor = JwtAuthExtractor(
            jwks_url=os.environ["ADK_CC_JWT_JWKS_URL"],
            issuer=os.environ.get("ADK_CC_JWT_ISSUER"),
            audience=os.environ.get("ADK_CC_JWT_AUDIENCE"),
            user_claim=os.environ.get("ADK_CC_JWT_USER_CLAIM", "sub"),
            tenant_claim=os.environ.get("ADK_CC_JWT_TENANT_CLAIM", "tenant"),
        )
    elif os.environ.get("ADK_CC_AUTH_TOKENS"):
        from .auth import BearerTokenExtractor

        extractor = BearerTokenExtractor()

    if extractor is None and os.environ.get("ADK_CC_ALLOW_NO_AUTH") != "1":
        raise RuntimeError(
            "make_app(): no auth extractor configured. Pick one:\n"
            "  - Set ADK_CC_JWT_JWKS_URL (and optionally ISSUER/AUDIENCE) "
            "for the production JwtAuthExtractor.\n"
            "  - Set ADK_CC_AUTH_TOKENS=token=user:tenant,... for the "
            "built-in dev BearerTokenExtractor.\n"
            "  - Implement an AuthExtractor and call "
            "build_fastapi_app(auth_extractor=...) from your own factory.\n"
            "  - For local dev only, set ADK_CC_ALLOW_NO_AUTH=1 to "
            "acknowledge the no-auth deployment."
        )

    ui_dist_dir: Optional[str] = None
    if os.environ.get("ADK_CC_SERVE_UI") == "1":
        explicit = os.environ.get("ADK_CC_UI_DIST")
        if explicit:
            ui_dist_dir = explicit
        else:
            # Default: <repo_root>/web/dist. server.py lives at
            # adk_cc/service/server.py, so repo_root is two parents up.
            ui_dist_dir = str(Path(__file__).resolve().parents[2] / "web" / "dist")

    return build_fastapi_app(
        agents_dir=agents_dir,
        session_service_uri=os.environ.get("ADK_CC_SESSION_DSN"),
        auth_extractor=extractor,
        ui_dist_dir=ui_dist_dir,
    )
