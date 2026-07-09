"""Desktop-local settings: layered global + per-project MCP servers, skills, and
secrets, plus global-only model endpoints.

Desktop runs single-user / no-auth, so the web ``/auth/*`` routes (which require
an identity provider + auth middleware) don't apply. These ``/desktop/settings/*``
routes manage the SAME underlying stores the agent reads, keyed so they map onto
the agent's tenant ∪ user union:

    scope=global   -> (tenant="local", user_id=None)        # tenant scope = shared
    scope=project  -> (tenant="local", user_id=<project id>)

During a turn in project P the desktop tenant resolver runs the agent as
``user_id=P``, so the agent already unions global (tenant) ∪ that project (user),
the project shadowing global by name (see service/registry.list_union and
credentials.get's personal→tenant fallback). Net: global applies everywhere,
per-project overrides for that project only.

Stores are wired by ``_prepare_admin_env`` (ADK_CC_TENANT_REGISTRY_DIR /
ADK_CC_TENANT_SKILLS_DIR / ADK_CC_MODEL_REGISTRY_FILE) and the encrypted-file
credential provider — the same ones the agent loads — so writes here are read
by the next turn.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

_log = logging.getLogger(__name__)

_TENANT = "local"
_MAX_SKILL_ZIP = 5 * 1024 * 1024  # 5 MiB
_MAX_SKILL_DIR = 20 * 1024 * 1024  # 20 MiB (uncompressed folder ingest)
# Junk never copied into the skill store (and excluded from the size budget).
_SKILL_IGNORE = {".git", "node_modules", "__pycache__", ".venv", ".DS_Store", ".mypy_cache"}


def _safe(value: str, label: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_")
    if not safe or safe != value:
        raise HTTPException(status_code=400, detail=f"unsafe {label}: {value!r}")
    return safe


def _scope_user(request: Request) -> Optional[str]:
    """Map the request's ?scope=global|project (&project_id=) to a credential/
    registry user_id: global → None (tenant scope); project → the project id
    (validated against the desktop project registry)."""
    scope = request.query_params.get("scope", "global")
    project_id = request.query_params.get("project_id")
    if scope == "global":
        return None
    if scope == "project":
        if not project_id:
            raise HTTPException(status_code=400, detail="project_id required for scope=project")
        from .desktop_routes import load_projects

        if not any(p.get("id") == project_id for p in load_projects()):
            raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
        return _safe(project_id, "project_id")
    raise HTTPException(status_code=400, detail="scope must be 'global' or 'project'")


def _invalidate_required_inputs() -> None:
    try:
        from ..credentials.required_inputs import invalidate_cache

        invalidate_cache()
    except Exception:  # noqa: BLE001 — best-effort cache bust
        pass


def mount_desktop_settings_routes(app) -> None:  # noqa: ANN001
    """Mount /desktop/settings/* when ADK_CC_DESKTOP=1; otherwise a no-op."""
    from .desktop_routes import desktop_enabled

    if not desktop_enabled():
        return

    # The same stores the agent reads (wired by _prepare_admin_env / the
    # credential provider). Any may be absent if its env knob isn't set — we
    # only mount the routes whose store exists.
    from ..credentials import credential_provider_from_env

    creds = credential_provider_from_env()

    mcp_reg = None
    reg_dir = os.environ.get("ADK_CC_TENANT_REGISTRY_DIR")
    if reg_dir:
        from .registry import JsonFileTenantResourceRegistry
        from ..tools.mcp import McpServerConfig

        mcp_reg = JsonFileTenantResourceRegistry(
            root=reg_dir, kind="mcp", model=McpServerConfig, id_attr="server_name"
        )

    skill_root = os.environ.get("ADK_CC_TENANT_SKILLS_DIR")

    models = None
    model_file = os.environ.get("ADK_CC_MODEL_REGISTRY_FILE")
    if model_file:
        from ..models.endpoints import ModelEndpointRegistry

        models = ModelEndpointRegistry(model_file)

    # ----------------------------------------------------------------- secrets
    if creds is not None:

        @app.get("/desktop/settings/secrets", include_in_schema=False)
        async def list_secrets(request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            keys = await creds.list_keys(tenant_id=_TENANT, user_id=uid)
            # For a project scope, also surface the globals it inherits.
            inherited = sorted(await creds.list_keys(tenant_id=_TENANT)) if uid else []
            return {"keys": sorted(keys), "inherited": inherited}

        @app.put("/desktop/settings/secrets/{key}", include_in_schema=False)
        async def put_secret(key: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            body = await request.json()
            value = str((body or {}).get("value", ""))
            if not value:
                raise HTTPException(status_code=400, detail="value required")
            await creds.put(tenant_id=_TENANT, key=_safe(key, "key"), value=value, user_id=uid)
            _invalidate_required_inputs()
            return {"status": "ok", "key": key}

        @app.delete("/desktop/settings/secrets/{key}", include_in_schema=False)
        async def delete_secret(key: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            await creds.delete(tenant_id=_TENANT, key=_safe(key, "key"), user_id=uid)
            _invalidate_required_inputs()
            return {"status": "deleted", "key": key}

    # --------------------------------------------------------------------- mcp
    if mcp_reg is not None:
        from ..tools.mcp import McpServerConfig

        @app.get("/desktop/settings/mcp", include_in_schema=False)
        async def list_mcp(request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            servers = await mcp_reg.list_for_tenant(_TENANT, uid)
            return {"servers": [s.model_dump(mode="json") for s in servers]}

        @app.put("/desktop/settings/mcp/{server_name}", include_in_schema=False)
        async def put_mcp(server_name: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            body = await request.json() or {}
            try:
                cfg = McpServerConfig(
                    server_name=_safe(server_name, "server_name"),
                    transport=str(body.get("transport") or "http"),
                    url=str(body.get("url") or ""),
                    credential_key=(body.get("credential_key") or None),
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid MCP config: {e}")
            await mcp_reg.add(tenant_id=_TENANT, resource=cfg, user_id=uid)
            return {"status": "ok", "server_name": server_name}

        @app.delete("/desktop/settings/mcp/{server_name}", include_in_schema=False)
        async def delete_mcp(server_name: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            await mcp_reg.remove(
                tenant_id=_TENANT, resource_id=_safe(server_name, "server_name"), user_id=uid
            )
            return {"status": "deleted", "server_name": server_name}

    # ------------------------------------------------------------------ skills
    if skill_root:
        sroot = Path(skill_root)

        def _skill_base(uid: Optional[str]) -> Path:
            # Mirrors tools/skills_tenant.py: global skills sit directly under the
            # tenant dir; per-project under <tenant>/_users/<project>.
            return sroot / _TENANT / "_users" / uid if uid else sroot / _TENANT

        @app.get("/desktop/settings/skills", include_in_schema=False)
        async def list_skills(request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            base = _skill_base(uid)
            if not base.is_dir():
                return {"skills": []}
            return {
                "skills": sorted(
                    p.name for p in base.iterdir() if p.is_dir() and p.name != "_users"
                )
            }

        @app.put("/desktop/settings/skills/{skill_name}", include_in_schema=False)
        async def put_skill(skill_name: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            s = _safe(skill_name, "skill_name")
            target = _skill_base(uid) / s
            body = await request.body()
            if not body:
                raise HTTPException(status_code=400, detail="empty body")
            if len(body) > _MAX_SKILL_ZIP:
                raise HTTPException(status_code=413, detail="skill zip too large")
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as zf:
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in Path(member).parts:
                            raise HTTPException(status_code=400, detail=f"unsafe path in zip: {member!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
                        zf.extractall(tmp)
                        if not any(
                            f.suffix.lower() in (".md", ".yaml", ".yml")
                            for f in Path(tmp).rglob("*")
                            if f.is_file()
                        ):
                            raise HTTPException(status_code=400, detail="no skill manifest (.md/.yaml) in zip")
                        if target.exists():
                            shutil.rmtree(target)
                        shutil.move(tmp, target)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="body is not a valid zip")
            _invalidate_required_inputs()
            return {"status": "ok", "skill_name": s}

        @app.delete("/desktop/settings/skills/{skill_name}", include_in_schema=False)
        async def delete_skill(skill_name: str, request: Request):  # noqa: ANN202
            uid = _scope_user(request)
            target = _skill_base(uid) / _safe(skill_name, "skill_name")
            if target.exists():
                shutil.rmtree(target)
            _invalidate_required_inputs()
            return {"status": "deleted", "skill_name": skill_name}

        @app.post("/desktop/settings/skills/from-dir", include_in_schema=False)
        async def add_skill_from_dir(request: Request):  # noqa: ANN202
            """Ingest a skill from a LOCAL directory (desktop is single-user loopback,
            so the server can read the picked path — same trust model as adding a
            project folder). Copies the tree into the skill store; skips junk; must
            contain a .md/.yaml manifest and stay under the size cap."""
            uid = _scope_user(request)
            body = await request.json() or {}
            raw = str(body.get("path") or "").strip()
            if not raw:
                raise HTTPException(status_code=400, detail="'path' required")
            src = Path(os.path.abspath(os.path.expanduser(raw)))
            if not src.is_dir():
                raise HTTPException(status_code=400, detail=f"not a directory: {src}")
            name = _safe(str(body.get("name") or src.name), "skill_name")

            def _kept(f: Path) -> bool:
                return f.is_file() and not any(part in _SKILL_IGNORE for part in f.relative_to(src).parts)

            files = [f for f in src.rglob("*") if _kept(f)]
            if not files:
                raise HTTPException(status_code=400, detail="folder is empty")
            if not any(f.suffix.lower() in (".md", ".yaml", ".yml") for f in files):
                raise HTTPException(status_code=400, detail="no skill manifest (.md/.yaml) in the folder")
            total = sum(f.stat().st_size for f in files)
            if total > _MAX_SKILL_DIR:
                raise HTTPException(status_code=413, detail="skill folder too large")

            target = _skill_base(uid) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
                dst = Path(tmp) / name
                shutil.copytree(src, dst, ignore=lambda _d, names: [n for n in names if n in _SKILL_IGNORE])
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(dst), str(target))
            _invalidate_required_inputs()
            return {"status": "ok", "skill_name": name}

    # ----------------------------------------------------- models (global only)
    if models is not None:
        from ..models.endpoints import ModelEndpointConfig

        @app.get("/desktop/settings/models", include_in_schema=False)
        async def list_models():  # noqa: ANN202
            return {"endpoints": [e.masked() for e in models.list()], "active": models.active_name()}

        @app.put("/desktop/settings/models/{name}", include_in_schema=False)
        async def put_model(name: str, request: Request):  # noqa: ANN202
            body = await request.json() or {}
            try:
                cfg = ModelEndpointConfig(
                    name=_safe(name, "name"),
                    model=str(body.get("model") or ""),
                    api_base=str(body.get("api_base") or ""),
                    api_key_env=str(body.get("api_key_env") or ""),
                    max_tokens=body.get("max_tokens"),
                    reasoning_effort=body.get("reasoning_effort"),
                    models=list(body.get("models") or []),
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid endpoint: {e}")
            models.upsert(cfg)
            return {"status": "ok", "name": name}

        async def _discover_into(cfg):  # noqa: ANN001, ANN202
            """Return a copy of `cfg` with its provider's models discovered and
            `model` normalised to one of them (keeps the current model if still
            offered, else the first). Best-effort — keeps existing on failure."""
            from ..models import discovery

            key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
            try:
                found = await discovery.list_provider_models(cfg.model, cfg.api_base, api_key=key)
            except Exception:  # noqa: BLE001 — provider unreachable / bad key
                found = list(cfg.models)
            model = cfg.model if cfg.model in found else (found[0] if found else cfg.model)
            return cfg.model_copy(update={"models": found, "model": model})

        @app.post("/desktop/settings/models/{name}/refresh-models", include_in_schema=False)
        async def refresh_models(name: str):  # noqa: ANN202
            cfg = models.get(_safe(name, "name"))
            if cfg is None:
                raise HTTPException(status_code=404, detail="unknown endpoint")
            updated = await _discover_into(cfg)
            models.upsert(updated)
            return updated.masked()

        @app.post("/desktop/settings/models/{name}/select-model", include_in_schema=False)
        async def select_model(name: str, request: Request):  # noqa: ANN202
            # Set a provider's active model (must be one it offers) AND activate
            # the provider — the shared "pick a model" action for the settings
            # dropdown and the /model command.
            body = await request.json() or {}
            m = str(body.get("model") or "")
            cfg = models.get(_safe(name, "name"))
            if cfg is None:
                raise HTTPException(status_code=404, detail="unknown endpoint")
            if m and cfg.models and m not in cfg.models:
                raise HTTPException(status_code=400, detail=f"{m!r} is not one of this provider's models")
            models.upsert(cfg.model_copy(update={"model": m or cfg.model}))
            try:
                models.activate(cfg.name)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=409, detail=str(e))
            return {"status": "ok", "active": cfg.name, "model": m or cfg.model}

        @app.delete("/desktop/settings/models/{name}", include_in_schema=False)
        async def delete_model(name: str):  # noqa: ANN202
            try:
                models.remove(_safe(name, "name"))
            except Exception as e:  # noqa: BLE001 — last/active endpoint guard
                raise HTTPException(status_code=409, detail=str(e))
            return {"status": "deleted", "name": name}

        @app.post("/desktop/settings/models/{name}/activate", include_in_schema=False)
        async def activate_model(name: str):  # noqa: ANN202
            try:
                models.activate(_safe(name, "name"))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=404, detail=str(e))
            return {"status": "ok", "active": name}

        @app.post("/desktop/settings/models/discover", include_in_schema=False)
        async def discover_models(request: Request):  # noqa: ANN202
            # List a provider's models via its OpenAI-compatible /models endpoint,
            # so the UI can offer a picker instead of a free-text model id.
            from ..models import discovery

            body = await request.json() or {}
            api_base = str(body.get("api_base") or "").strip()
            if not api_base:
                raise HTTPException(status_code=400, detail="api_base required")
            key_env = str(body.get("api_key_env") or "")
            api_key = os.environ.get(key_env) if key_env else None
            try:
                found = await discovery.list_models(api_base, api_key=api_key)
            except Exception as e:  # noqa: BLE001 — provider unreachable / bad key
                raise HTTPException(status_code=502, detail=f"could not list models: {e}")
            return {"models": found}

        # ------------------------------------------- ChatGPT subscription (Codex)
        # Phase 1: reuse the user's existing `codex login` (~/.codex/auth.json).
        # Connect = register + activate a `chatgpt-codex/<model>` endpoint; the
        # SUBSCRIPTION Bearer token (never an API key) is read at request time by
        # the provider. No token is stored in the registry or returned over HTTP.
        _CODEX_ENDPOINT = "chatgpt-codex"

        def _codex_state() -> dict:
            from ..models import codex_auth

            status = codex_auth.connection_status()
            ep = models.get(_CODEX_ENDPOINT)
            status["registered"] = ep is not None
            status["active"] = models.active_name() == _CODEX_ENDPOINT
            status["model"] = ep.model.split("/", 1)[1] if ep else None
            status["models"] = [m.split("/", 1)[-1] for m in ep.models] if ep else []
            return status

        @app.get("/desktop/settings/codex", include_in_schema=False)
        async def codex_status():  # noqa: ANN202
            return _codex_state()

        @app.get("/desktop/settings/codex/models", include_in_schema=False)
        async def codex_models():  # noqa: ANN202
            from ..models import codex_auth, discovery

            try:
                found = await discovery.list_models(discovery.CODEX_BASE, use_codex_auth=True)
            except codex_auth.CodexAuthError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"could not list models: {e}")
            # Keep the general chat models; drop internal/non-chat slugs
            # (codex-auto-review) and *-codex variants, which 400 for ChatGPT
            # accounts (like gpt-5.1-codex did). Fall back to the raw list if the
            # filter would empty it (so a future naming change never hides all).
            chat = [m for m in found if "review" not in m and "codex" not in m]
            return {"models": chat or found}

        @app.post("/desktop/settings/codex/connect", include_in_schema=False)
        async def codex_connect(request: Request):  # noqa: ANN202
            from ..models import codex_auth

            status = codex_auth.connection_status()
            if not status.get("connected"):
                raise HTTPException(
                    status_code=400,
                    detail="No ChatGPT login found. Run `codex login` first, then connect.",
                )
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001 — empty/invalid body is fine
                body = {}
            requested = (body or {}).get("model")
            effort = (body or {}).get("reasoning_effort") or "medium"
            cfg = ModelEndpointConfig(
                name=_CODEX_ENDPOINT,
                model=f"chatgpt-codex/{requested}" if requested else "chatgpt-codex/gpt-5.5",
                api_base="https://chatgpt.com/backend-api/codex",
                api_key_env="",  # subscription token, not an env key
                reasoning_effort=str(effort),
            )
            # Discover the account's models; with no explicit request, default to
            # the first (per the "sets first in entry as default" flow).
            cfg = await _discover_into(cfg)
            if not requested and cfg.models:
                cfg = cfg.model_copy(update={"model": cfg.models[0]})
            models.upsert(cfg)
            models.activate(_CODEX_ENDPOINT)
            return _codex_state()

        def _drop_codex_endpoint() -> None:
            if models.active_name() == _CODEX_ENDPOINT:
                other = next((e.name for e in models.list() if e.name != _CODEX_ENDPOINT), None)
                if other:
                    models.activate(other)
            try:
                models.remove(_CODEX_ENDPOINT)
            except Exception as e:  # noqa: BLE001 — last-endpoint guard
                raise HTTPException(status_code=409, detail=str(e))

        @app.post("/desktop/settings/codex/disconnect", include_in_schema=False)
        async def codex_disconnect(request: Request):  # noqa: ANN202
            # Stop using ChatGPT as the model. The stored login (CLI or ours) is
            # kept so re-connecting is one click; use sign-out to clear our login.
            _drop_codex_endpoint()
            return {"status": "disconnected"}

        # -- Phase 2: our own "Sign in with ChatGPT" (browser PKCE, localhost:1455)
        @app.post("/desktop/settings/codex/login/start", include_in_schema=False)
        async def codex_login_start():  # noqa: ANN202
            from ..models import codex_oauth

            try:
                return {"auth_url": codex_oauth.start()}
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=409, detail=str(e))

        @app.get("/desktop/settings/codex/login/status", include_in_schema=False)
        async def codex_login_status():  # noqa: ANN202
            from ..models import codex_oauth

            return codex_oauth.status()

        @app.post("/desktop/settings/codex/signout", include_in_schema=False)
        async def codex_signout():  # noqa: ANN202
            # Clear OUR OWN stored login (Phase-2) and drop the endpoint. The
            # Codex CLI login (~/.codex) is never touched.
            from ..models import codex_auth

            codex_auth.clear_login()
            if models.get(_CODEX_ENDPOINT) is not None:
                _drop_codex_endpoint()
            return _codex_state()

    # ------------------------------------------------- working directories
    # Persistent per-project "granted" directories (Claude Code's
    # additionalDirectories). Stored as `adk_cc_extra_roots` in the project's
    # shared user-state, which `get_workspace` folds into the sandbox scope for
    # every session of that project. Added via `/add-dir` or the Settings UI.
    def _fss():  # noqa: ANN202
        from .. import deployment
        from .file_session_service import FileSessionService

        return FileSessionService(deployment.desktop_data_dir())

    def _require_project(request: Request) -> str:
        uid = _scope_user(request)
        if not uid:
            raise HTTPException(
                status_code=400,
                detail="working directories are per-project (scope=project&project_id=…)",
            )
        return uid

    @app.get("/desktop/working-dirs", include_in_schema=False)
    async def list_working_dirs(request: Request):  # noqa: ANN202
        pid = _require_project(request)
        from .desktop_routes import project_repo_path

        roots = _fss().get_user_value(pid, "adk_cc_extra_roots", []) or []
        return {"project_root": project_repo_path(pid), "dirs": list(roots)}

    @app.post("/desktop/working-dirs", include_in_schema=False)
    async def add_working_dir(request: Request):  # noqa: ANN202
        pid = _require_project(request)
        body = await request.json() or {}
        raw = str(body.get("path") or "").strip()
        if not raw:
            raise HTTPException(status_code=400, detail="path required")
        p = os.path.realpath(os.path.expanduser(raw))
        if not os.path.isdir(p):
            raise HTTPException(status_code=400, detail=f"not a directory: {raw}")
        fss = _fss()
        cur = list(fss.get_user_value(pid, "adk_cc_extra_roots", []) or [])
        if p not in cur:
            cur.append(p)
            fss.set_user_value(pid, "adk_cc_extra_roots", cur)
        return {"status": "ok", "dirs": cur}

    @app.delete("/desktop/working-dirs", include_in_schema=False)
    async def remove_working_dir(request: Request):  # noqa: ANN202
        pid = _require_project(request)
        body = await request.json() or {}
        raw = str(body.get("path") or "").strip()
        p = os.path.realpath(os.path.expanduser(raw)) if raw else raw
        fss = _fss()
        cur = [r for r in (fss.get_user_value(pid, "adk_cc_extra_roots", []) or []) if r != p]
        fss.set_user_value(pid, "adk_cc_extra_roots", cur)
        return {"status": "deleted", "dirs": cur}

    _log.info(
        "desktop settings routes mounted (secrets=%s mcp=%s skills=%s models=%s)",
        creds is not None, mcp_reg is not None, bool(skill_root), models is not None,
    )
