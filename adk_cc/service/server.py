"""FastAPI server factory.

Wraps ADK's `get_fast_api_app` with adk-cc's plugin stack and (optional)
auth middleware. Operators run this via uvicorn:

    uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000

Or programmatically:

    from adk_cc.service.server import build_fastapi_app
    app = build_fastapi_app(
        agents_dir="/path/to/agents",
        session_service_uri="postgresql://user:pass@host/db",
        permission_settings_yaml="/etc/adk-cc/permissions.yaml",
    )

The function builds the plugin stack in the documented order
([AuditPlugin, TenancyPlugin, PermissionPlugin, QuotaPlugin]) and passes
them as `extra_plugins`. ADK takes care of session storage, FastAPI
routing, and lifecycle management; we add policy + auth on top.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from ..permissions import PermissionMode, SettingsHierarchy
from ..plugins import (
    AuditPlugin,
    PermissionPlugin,
    PlanModeReminderPlugin,
    QuotaPlugin,
    TaskReminderPlugin,
)
from .tenancy import TenancyPlugin


def build_plugins(
    *,
    permission_settings: Optional[SettingsHierarchy] = None,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
    audit_log_path: Optional[str] = None,
    quota_per_minute: int = 120,
    workspace_root: Optional[str] = None,
) -> list:
    """Construct the production plugin stack in the documented order.

    Order rationale:
      Audit first  → records every attempt, even ones the rest deny.
      Tenancy next → seeds session state so later plugins can read tenant.
      Permission   → policy gate.
      Quota last   → only counts calls that pass the gate.
    """
    return [
        AuditPlugin(sink=audit_log_path) if audit_log_path else AuditPlugin(),
        TenancyPlugin(default_workspace_root=workspace_root),
        PermissionPlugin(
            permission_settings or SettingsHierarchy.empty(),
            default_mode=permission_mode,
        ),
        QuotaPlugin(calls_per_minute=quota_per_minute),
        # Reminders run at before_model_callback; sit beside the
        # before_tool chain rather than inside it.
        PlanModeReminderPlugin(),
        TaskReminderPlugin(),
    ]


def build_fastapi_app(
    *,
    agents_dir: str,
    session_service_uri: Optional[str] = None,
    permission_settings_yaml: Optional[str] = None,
    permission_mode: Optional[PermissionMode] = None,
    audit_log_path: Optional[str] = None,
    quota_per_minute: int = 120,
    workspace_root: Optional[str] = None,
    auth_extractor=None,
    serve_web: bool = False,
):
    """Build a production FastAPI app.

    Args:
      agents_dir: AGENTS_DIR for ADK's loader (parent of `adk_cc/`).
      session_service_uri: e.g. "postgresql://...", "sqlite:///./adk-cc.db".
        None → in-memory (dev only).
      permission_settings_yaml: path to a YAML rules file; None → empty rules.
      permission_mode: defaults to env `ADK_CC_PERMISSION_MODE` or DEFAULT.
      audit_log_path: JSONL path; None → ~/.adk-cc/audit.jsonl.
      quota_per_minute: per-tenant tool-call rate cap.
      workspace_root: per-session FS root; None → CWD.
      auth_extractor: callable(request) → (user_id, tenant_id); None → no auth.
      serve_web: True to also mount the web UI; False = API only.
    """
    from google.adk.cli.fast_api import get_fast_api_app

    settings = (
        SettingsHierarchy.empty()
        if permission_settings_yaml is None
        else _load_settings(permission_settings_yaml)
    )
    if permission_mode is None:
        permission_mode = PermissionMode(
            os.environ.get("ADK_CC_PERMISSION_MODE", PermissionMode.DEFAULT.value)
        )

    plugins = build_plugins(
        permission_settings=settings,
        permission_mode=permission_mode,
        audit_log_path=audit_log_path,
        quota_per_minute=quota_per_minute,
        workspace_root=workspace_root,
    )

    fastapi_app = get_fast_api_app(
        agents_dir=agents_dir,
        session_service_uri=session_service_uri,
        extra_plugins=plugins,
        web=serve_web,
    )

    if auth_extractor is not None:
        from .auth import make_auth_middleware

        fastapi_app.add_middleware(make_auth_middleware(auth_extractor))

    return fastapi_app


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

    Auth extractor selection (first match wins):
      1. ADK_CC_JWT_JWKS_URL set → JwtAuthExtractor (production).
      2. ADK_CC_AUTH_TOKENS set → BearerTokenExtractor (dev).
      3. Otherwise fail-closed unless ADK_CC_ALLOW_NO_AUTH=1.

    Operators with bespoke auth (session DB, mTLS, custom IdP integration)
    should implement an `AuthExtractor` and call `build_fastapi_app(
    auth_extractor=...)` from their own factory rather than `make_app`.
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

    return build_fastapi_app(
        agents_dir=agents_dir,
        session_service_uri=os.environ.get("ADK_CC_SESSION_DSN"),
        permission_settings_yaml=os.environ.get("ADK_CC_PERMISSIONS_YAML"),
        audit_log_path=os.environ.get("ADK_CC_AUDIT_LOG"),
        quota_per_minute=int(os.environ.get("ADK_CC_QUOTA_PER_MINUTE", "120")),
        workspace_root=os.environ.get("ADK_CC_WORKSPACE_ROOT"),
        auth_extractor=extractor,
    )


def _load_settings(yaml_path: str) -> SettingsHierarchy:
    from ..config import load_settings_from_yaml

    return load_settings_from_yaml(yaml_path)
