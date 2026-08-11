"""Opt-in FastAPI routes for tenant-resource administration.

`make_app()` does NOT mount these by default. Operators who want HTTP
admin (typically a SaaS deployment where tenants self-serve) call:

    from adk_cc.service.admin_routes import mount_tenant_admin
    app = build_fastapi_app(...)
    mount_tenant_admin(
        app,
        registry=...,                 # TenantResourceRegistry[McpServerConfig]
        credentials=...,              # CredentialProvider
        skill_root="/srv/skills",     # optional; enables skill upload
        admin_extractor=my_admin_auth,  # optional; defaults to the app's auth
    )

Operators with a different control surface (CLI, file-watching config
loader, programmatic API) call the underlying `CredentialProvider` /
`TenantResourceRegistry` methods directly and skip this module.

Routes mounted (auth gate uses the app's auth middleware unless an
`admin_extractor` is supplied):

  - GET    /tenants/{tid}/credentials                  → list keys (no values)
  - PUT    /tenants/{tid}/credentials/{key}            → upsert {value}
  - DELETE /tenants/{tid}/credentials/{key}            → remove

  - GET    /tenants/{tid}/mcp-servers                  → list configs
  - PUT    /tenants/{tid}/mcp-servers/{server_name}    → upsert config
  - DELETE /tenants/{tid}/mcp-servers/{server_name}    → remove

  - GET    /tenants/{tid}/skills                       → list skill names
  - PUT    /tenants/{tid}/skills/{skill_name}          → upload zip body
  - DELETE /tenants/{tid}/skills/{skill_name}          → remove

The credential GET intentionally does NOT return values — it lists
which keys are registered. Use the underlying CredentialProvider
programmatically if you need to read a value (e.g. for migration).
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Awaitable, Callable, Optional

# fastapi is required when this module is used; import at top-level
# so FastAPI's signature introspection can resolve Request annotations.
from fastapi import APIRouter, HTTPException, Request

from ..credentials import CredentialProvider
from ..tools.mcp_tenant import McpServerConfig
from .registry import TenantResourceRegistry
from ..config.schema import env_bool


def _ensure_safe_id(value: str, label: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if safe != value or not safe:
        raise HTTPException(status_code=400, detail=f"unsafe {label}: {value!r}")
    return safe


def _authorize_for_tenant(request, target_tenant: str) -> None:
    """Default RBAC: caller's tenant must match target tenant.

    Operators with different rules (e.g. a global admin claim) override
    via `admin_extractor` when calling `mount_tenant_admin`.
    """
    auth = getattr(request.state, "adk_cc_auth", None)
    if auth is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    user_id, caller_tenant = auth
    if caller_tenant != target_tenant:
        raise HTTPException(
            status_code=403, detail="cannot manage another tenant's resources"
        )


def mount_tenant_admin(
    app,
    *,
    registry: TenantResourceRegistry[McpServerConfig],
    credentials: CredentialProvider,
    skill_root: Optional[str] = None,
    admin_extractor: Optional[Callable[..., Awaitable[None]]] = None,
) -> None:
    """Mount tenant CRUD routes on a FastAPI app.

    Args:
      app: FastAPI app from `build_fastapi_app`.
      registry: where MCP server configs live.
      credentials: where credentials live.
      skill_root: enables skill upload routes if set; expected layout
        is `<skill_root>/<tenant_id>/<skill_name>/`.
      admin_extractor: replaces the default same-tenant authorize check.
        Receives `request` and `target_tenant_id`; raises HTTPException
        on denial. Default uses the app's auth middleware result.
    """
    router = APIRouter(prefix="/tenants/{tenant_id}")
    authorize = admin_extractor or _authorize_for_tenant

    # --- Credentials --------------------------------------------------

    @router.get("/credentials")
    async def list_creds(tenant_id: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        # Key NAMES only — values are never exposed over HTTP. The ABC
        # default returns [] (external providers opt in by overriding
        # list_keys); the stock in-memory + encrypted-file providers list.
        keys = await credentials.list_keys(tenant_id=tenant_id)
        return {"keys": keys}

    @router.put("/credentials/{key}")
    async def put_cred(tenant_id: str, key: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        _ensure_safe_id(tenant_id, "tenant_id")
        _ensure_safe_id(key, "credential key")
        body = await request.json()
        if "value" not in body or not isinstance(body["value"], str):
            raise HTTPException(status_code=400, detail="missing string field 'value'")
        await credentials.put(tenant_id=tenant_id, key=key, value=body["value"])
        return {"status": "ok"}

    @router.delete("/credentials/{key}")
    async def delete_cred(tenant_id: str, key: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        await credentials.delete(tenant_id=tenant_id, key=key)
        return {"status": "ok"}

    # --- MCP servers --------------------------------------------------

    @router.get("/mcp-servers")
    async def list_mcp(tenant_id: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        configs = await registry.list_for_tenant(tenant_id)
        return {"servers": [c.model_dump(mode="json") for c in configs]}

    @router.put("/mcp-servers/{server_name}")
    async def put_mcp(tenant_id: str, server_name: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        body = await request.json()
        body["server_name"] = server_name  # path wins over body
        try:
            cfg = McpServerConfig.model_validate(body)
        except Exception as e:  # noqa: BLE001 — Pydantic validation
            raise HTTPException(status_code=400, detail=f"invalid config: {e}")
        await registry.add(tenant_id=tenant_id, resource=cfg)
        return {"status": "ok"}

    @router.delete("/mcp-servers/{server_name}")
    async def delete_mcp(tenant_id: str, server_name: str, request: Request):
        await _maybe_await(authorize(request, tenant_id))
        await registry.remove(tenant_id=tenant_id, resource_id=server_name)
        return {"status": "ok"}

    # --- Skills (only mounted if skill_root configured) --------------

    if skill_root is not None:
        skill_root_path = Path(skill_root)

        @router.get("/skills")
        async def list_skills(tenant_id: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            tenant_dir = skill_root_path / _ensure_safe_id(tenant_id, "tenant_id")
            if not tenant_dir.is_dir():
                return {"skills": []}
            return {"skills": sorted(p.name for p in tenant_dir.iterdir() if p.is_dir())}

        @router.get("/skills/catalog")
        async def skills_catalog(tenant_id: str, request: Request):
            """Every skill the tenant's agents can see (org uploads + built-ins)
            with its org-level on/off state."""
            from ..tools import skill_enablement

            await _maybe_await(authorize(request, tenant_id))
            t = _ensure_safe_id(tenant_id, "tenant_id")
            return {
                "skills": skill_enablement.catalog(
                    tenant_id=tenant_id,
                    scoped_dirs=[("org", skill_root_path / t)],
                )
            }

        @router.patch("/skills/{skill_name}/enabled")
        async def set_skill_enabled(tenant_id: str, skill_name: str, request: Request):
            """Org-wide switch: applies to every user in the tenant, and is a
            FLOOR — no per-user or per-chat setting can lift it (see
            tools/skill_enablement.disabled_names)."""
            from ..tools import skill_enablement

            await _maybe_await(authorize(request, tenant_id))
            s = _ensure_safe_id(skill_name, "skill_name")
            body = await request.json() or {}
            if "enabled" not in body:
                raise HTTPException(status_code=400, detail="'enabled' required")
            enabled = bool(body["enabled"])
            skill_enablement.set_enabled(s, enabled, tenant_id=tenant_id)
            return {"status": "ok", "skill_name": s, "enabled": enabled}

        @router.put("/skills/{skill_name}")
        async def put_skill(tenant_id: str, skill_name: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            t = _ensure_safe_id(tenant_id, "tenant_id")
            s = _ensure_safe_id(skill_name, "skill_name")
            target = skill_root_path / t / s

            body = await request.body()
            if not body:
                raise HTTPException(status_code=400, detail="empty body")
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    # Atomic install: extract to temp dir, then rename.
                    with tempfile.TemporaryDirectory(
                        dir=str(skill_root_path / t) if (skill_root_path / t).is_dir() else None
                    ) as tmp:
                        # Reject zip entries that would escape the target dir.
                        for member in zf.namelist():
                            if member.startswith("/") or ".." in Path(member).parts:
                                raise HTTPException(
                                    status_code=400,
                                    detail=f"unsafe path in zip: {member!r}",
                                )
                        zf.extractall(tmp)
                        # Validate at least one frontmatter-bearing file exists.
                        if not any(
                            f.suffix.lower() in (".md", ".yaml", ".yml")
                            for f in Path(tmp).rglob("*")
                            if f.is_file()
                        ):
                            raise HTTPException(
                                status_code=400, detail="no skill manifest found in zip"
                            )
                        # Rename into place atomically.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.move(tmp, target)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="body is not a valid zip")
            return {"status": "ok"}

        @router.delete("/skills/{skill_name}")
        async def delete_skill(tenant_id: str, skill_name: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            t = _ensure_safe_id(tenant_id, "tenant_id")
            s = _ensure_safe_id(skill_name, "skill_name")
            target = skill_root_path / t / s
            if target.exists():
                shutil.rmtree(target)
            return {"status": "ok"}

    # --- Wiki memory settings (only mounted when ADK_CC_WIKI=1) -------
    # Per-tenant, admin-tunable knobs for the knowledge wiki. Today: the
    # corroboration threshold N (how many independent users must corroborate
    # a claim to overturn a domain fact without human adjudication). Stored
    # in the tenant's wiki settings.json; the librarian reads it each run.
    if env_bool("ADK_CC_WIKI"):
        from ..wiki import WikiStore

        @router.get("/wiki-settings")
        async def get_wiki_settings(tenant_id: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            store = WikiStore.for_tenant(tenant_id)
            return {
                "settings": store.read_settings(),
                "effective": {"corroboration_n": store.corroboration_n},
            }

        @router.put("/wiki-settings/corroboration_n")
        async def put_corroboration_n(tenant_id: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            _ensure_safe_id(tenant_id, "tenant_id")
            body = await request.json()
            value = body.get("value")
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="'value' must be an integer ≥ 1"
                )
            if n < 1:
                raise HTTPException(status_code=400, detail="corroboration_n must be ≥ 1")
            store = WikiStore.for_tenant(tenant_id).ensure()
            store.set_setting("corroboration_n", n)
            return {"status": "ok", "corroboration_n": store.corroboration_n}

        # --- Wiki document management (#129) --------------------------
        # The domain wiki previously had NO human management surface: the
        # librarian was the only writer. These routes let an admin/owner
        # edit or delete pages and adjudicate the review queue. LOAD-
        # BEARING: an edited page is stamped `human_edited`, and the
        # librarian holds any would-be supersession of such a page for
        # human adjudication — a manual correction survives every run.
        def _auth_user(request) -> str:
            auth = getattr(request.state, "adk_cc_auth", None)
            uid = getattr(auth, "user_id", None)
            if uid:
                return str(uid)
            try:
                return str(auth[0]) if auth else "admin"
            except Exception:  # noqa: BLE001
                return "admin"

        @router.get("/wiki-pages")
        async def list_wiki_pages(tenant_id: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            store = WikiStore.for_tenant(tenant_id)
            pages = []
            for slug in store.list_domain_pages():
                p = store.read_domain_page(slug)
                if p is None:
                    continue
                pages.append({
                    "slug": slug,
                    "title": p.frontmatter.get("title") or slug,
                    "contested": p.contested,
                    "human_edited": p.frontmatter.get("human_edited") or None,
                })
            return {"pages": pages}

        @router.get("/wiki-pages/{slug}")
        async def get_wiki_page(tenant_id: str, slug: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            store = WikiStore.for_tenant(tenant_id)
            p = store.read_domain_page(_ensure_safe_id(slug, "slug"))
            if p is None:
                raise HTTPException(status_code=404, detail="page not found")
            return {"slug": slug, "frontmatter": p.frontmatter, "body": p.body}

        @router.put("/wiki-pages/{slug}")
        async def put_wiki_page(tenant_id: str, slug: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            slug = _ensure_safe_id(slug, "slug")
            body = await request.json()
            text = body.get("body")
            if not isinstance(text, str) or not text.strip():
                raise HTTPException(status_code=400,
                                    detail="'body' (non-empty string) required")
            from ..wiki.page import Page
            from datetime import datetime, timezone

            store = WikiStore.for_tenant(tenant_id).ensure()
            existing = store.read_domain_page(slug)
            fm = dict(existing.frontmatter) if existing else {"title": slug}
            patch = body.get("frontmatter")
            if isinstance(patch, dict):
                for k, v in patch.items():
                    if v is None:
                        fm.pop(k, None)
                    else:
                        fm[k] = v
            editor = _auth_user(request)
            fm["human_edited"] = datetime.now(timezone.utc).isoformat()
            fm["edited_by"] = editor
            store.write_domain_page(Page(slug=slug, frontmatter=fm, body=text))
            store.append_changelog(
                {"op": "human_edit", "slug": slug, "by": editor})
            return {"status": "ok", "slug": slug,
                    "human_edited": fm["human_edited"]}

        @router.delete("/wiki-pages/{slug}")
        async def delete_wiki_page(tenant_id: str, slug: str, request: Request):
            await _maybe_await(authorize(request, tenant_id))
            slug = _ensure_safe_id(slug, "slug")
            store = WikiStore.for_tenant(tenant_id)
            gone = store.delete_domain_page(slug)
            if gone is None:
                raise HTTPException(status_code=404, detail="page not found")
            store.append_changelog(
                {"op": "human_delete", "slug": slug, "by": _auth_user(request)})
            return {"status": "deleted", "slug": slug}

        @router.get("/wiki-review")
        async def list_wiki_review(tenant_id: str, request: Request,
                                   pending_only: bool = True):
            await _maybe_await(authorize(request, tenant_id))
            store = WikiStore.for_tenant(tenant_id)
            contested = []
            for slug in store.list_domain_pages():
                p = store.read_domain_page(slug)
                if p is not None and p.contested:
                    contested.append(slug)
            return {"queue": store.list_quarantine(pending_only=pending_only),
                    "contested_pages": contested}

        @router.post("/wiki-review/{claim_hash}")
        async def adjudicate_claim(tenant_id: str, claim_hash: str,
                                   request: Request):
            await _maybe_await(authorize(request, tenant_id))
            body = await request.json()
            action = str(body.get("action") or "")
            if action not in ("accept", "reject"):
                raise HTTPException(status_code=400,
                                    detail="'action' must be accept|reject")
            store = WikiStore.for_tenant(tenant_id).ensure()
            rec = store.adjudicate(
                claim_hash, action=action,
                note=str(body.get("note") or ""), by=_auth_user(request))
            return {"status": "ok", "record": rec}

    app.include_router(router)


def mount_model_admin(
    app,
    *,
    registry,  # ModelEndpointRegistry
    authorize: Callable[..., object],
) -> None:
    """Mount model-endpoint admin routes (GLOBAL — the model is one
    deployment-wide resource, not tenant-scoped).

    Routes (under /admin/model-endpoints):
      - GET    /admin/model-endpoints              → list (secrets masked) + active
      - PUT    /admin/model-endpoints/{name}       → upsert {model, api_base, api_key? ("" = keyless), api_key_env? (legacy)}
      - DELETE /admin/model-endpoints/{name}       → remove (guards last/active)
      - POST   /admin/model-endpoints/{name}/activate → set active

    `authorize(request, target)` is the same admin gate used for tenant
    routes; here `target` is the literal "model-endpoints" scope string.
    """
    from ..models import ModelEndpointConfig

    router = APIRouter(prefix="/admin/model-endpoints")

    @router.get("")
    async def list_endpoints(request: Request):
        await _maybe_await(authorize(request, "model-endpoints"))
        return {
            "endpoints": [e.masked() for e in registry.list()],
            "active": registry.active_name(),
        }

    @router.put("/{name}")
    async def put_endpoint(name: str, request: Request):
        await _maybe_await(authorize(request, "model-endpoints"))
        _ensure_safe_id(name, "endpoint name")
        body = await request.json()
        body["name"] = name  # path wins
        # api_key is write-only (masked in every GET), so an update that omits
        # it keeps the stored key instead of wiping it. Explicit "" = keyless.
        if "api_key" not in body:
            existing = registry.get(name)
            if existing is not None and existing.api_key is not None:
                body["api_key"] = existing.api_key
        try:
            cfg = ModelEndpointConfig.model_validate(body)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid endpoint: {e}")
        registry.upsert(cfg)
        return {"status": "ok"}

    @router.delete("/{name}")
    async def delete_endpoint(name: str, request: Request):
        await _maybe_await(authorize(request, "model-endpoints"))
        try:
            registry.remove(name)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "ok"}

    @router.post("/{name}/activate")
    async def activate_endpoint(name: str, request: Request):
        await _maybe_await(authorize(request, "model-endpoints"))
        try:
            registry.activate(name)
        except ValueError as e:
            # Unknown endpoint → 404; a known endpoint that can't be activated
            # (e.g. its api_key_env is unset) → 409 Conflict, an actionable
            # config error rather than "not found".
            status = 404 if str(e).startswith("unknown endpoint") else 409
            raise HTTPException(status_code=status, detail=str(e))
        return {"status": "ok", "active": name}

    app.include_router(router)


async def _maybe_await(value):
    """Authorize hook may be sync or async; normalize."""
    import inspect

    if inspect.isawaitable(value):
        await value
