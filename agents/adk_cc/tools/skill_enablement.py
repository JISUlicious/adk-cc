"""Turn individual skills off without uninstalling them (W8).

Today a skill is all-or-nothing: the only way to stop one is to delete it. That
is wrong for built-ins (a user cannot delete what ships in the wheel) and
wasteful for the rest — every discovered skill's name + description is injected
into EVERY request's system instruction by `SkillToolset.process_llm_request`,
so an unwanted skill costs tokens and selection precision on every single turn.

Model: a **deny-list of skill names**. Names, not paths, so one entry covers a
skill regardless of which source it came from (built-in, project, user, org) and
keeps working if it moves. A deny-list also means newly installed skills default
to ON — the behaviour that exists today.

Three scopes, resolved per call (precedence low → high):

    org/tenant deny-list   — an admin turns it off for everyone
    user deny-list         — Settings, "I never want this one"
    session override       — "not in this chat", or "yes, just here"

The session layer is a dict `{name: bool}` rather than a second deny-list
precisely so it can also turn something back ON that a broader scope disabled.

Why per call and not at boot: `_skills = make_skill_toolset()` is built ONCE at
module import and shared by every session and user (agent.py), so enablement
cannot be baked into the toolset. Everything here is therefore a cheap, pure
lookup keyed on the session state that the caller already has.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

_log = logging.getLogger(__name__)

# Session-state key holding {skill_name: enabled}. `temp:` is deliberately NOT
# used — a per-chat toggle should survive the invocation that set it.
STATE_OVERRIDES = "skill_overrides"

_FILE_ENV = "ADK_CC_SKILL_ENABLEMENT_FILE"
_lock = threading.Lock()


def store_path() -> Path:
    """Where the deny-lists live. One file, all tenants — it is a tiny name
    list, and keeping it out of the skill directories means enablement survives
    uninstall/reinstall of the skill itself."""
    override = os.environ.get(_FILE_ENV)
    if override:
        return Path(override).expanduser()
    from ..deployment import data_dir

    return data_dir() / "skill-enablement.json"


def _read() -> dict:
    path = store_path()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # corrupt file must not break the agent
        _log.warning("skill enablement: unreadable %s (%s) — treating as empty", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic — a torn file would disable random skills


def _tenant_node(data: dict, tenant_id: str, *, create: bool = False) -> dict:
    tenants = data.setdefault("tenants", {}) if create else data.get("tenants") or {}
    node = tenants.setdefault(tenant_id, {}) if create else tenants.get(tenant_id) or {}
    return node


def disabled_for(
    tenant_id: Optional[str] = None, user_id: Optional[str] = None
) -> tuple[frozenset[str], frozenset[str]]:
    """`(org_disabled, user_disabled)` — the persisted layers, unmerged.

    Kept separate so the UI can say WHY a skill is off: "your org disabled this"
    reads very differently from "you disabled this", and only the second is
    something the user can undo.
    """
    data = _read()
    tid = tenant_id or _default_tenant()
    node = _tenant_node(data, tid)
    org = frozenset(node.get("disabled") or ())
    user = frozenset()
    if user_id:
        users = node.get("users") or {}
        user = frozenset((users.get(user_id) or {}).get("disabled") or ())
    return org, user


def set_enabled(
    name: str,
    enabled: bool,
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Persist one toggle. Unknown names are allowed on purpose: a skill can be
    disabled before it is installed (or while its source is offline), and the
    deny-list should not silently drop the entry when the skill reappears."""
    if not name:
        raise ValueError("skill name is required")
    tid = tenant_id or _default_tenant()
    with _lock:
        data = _read()
        node = _tenant_node(data, tid, create=True)
        if user_id:
            node = node.setdefault("users", {}).setdefault(user_id, {})
        current = list(node.get("disabled") or ())
        if enabled:
            current = [n for n in current if n != name]
        elif name not in current:
            current.append(name)
        node["disabled"] = sorted(current)
        _write(data)


def _default_tenant() -> str:
    """Desktop and single-tenant deployments have no tenant context; give them a
    stable key rather than a null one so the file has a single shape.

    Reads the EXISTING `ADK_CC_GLOBAL_TENANT_ID` rather than inventing a second
    name for the same idea — a synonym would have to be kept in sync forever,
    and the config schema exists to stop exactly that.
    """
    return os.environ.get("ADK_CC_GLOBAL_TENANT_ID") or "local"


def _scope_from_state(state: Any) -> tuple[Optional[str], Optional[str]]:
    """Tenant/user out of ADK session state, the same way TenantSkillToolset
    reads it. Best-effort: no context = default tenant, no user scope."""
    try:
        ctx = state.get("temp:tenant_context")
    except Exception:  # noqa: BLE001 — state shapes vary across deployments
        return None, None
    if ctx is None:
        return None, None
    return getattr(ctx, "tenant_id", None), getattr(ctx, "user_id", None)


def _overrides_from_state(state: Any) -> dict[str, bool]:
    try:
        raw = state.get(STATE_OVERRIDES)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


def disabled_names(state: Any) -> frozenset[str]:
    """The effective deny-list for one session. The only function the tool layer
    needs; everything else here serves the API and the UI.

    The org layer is a FLOOR only where it belongs to someone else: a multi-user
    deployment where an admin disabled a skill tenant-wide. Then no per-user or
    per-chat setting can lift it, the same way the protected-path permission
    floor works.

    Everywhere else the tenant-scope list is the SAME person's own global
    setting — desktop's `scope=global`, or a single-user server with no identity
    at all — and "off globally, except in this chat" has to work. Hence the two
    conditions rather than one.
    """
    tenant_id, user_id = _scope_from_state(state)
    org, user = disabled_for(tenant_id, user_id)
    admin_scope = tenant_id is not None and not _org_layer_is_self()
    floor = org if admin_scope else frozenset()

    effective = set(user) | (set(org) - set(floor))
    for name, on in _overrides_from_state(state).items():
        if on:
            effective.discard(name)
        else:
            effective.add(name)
    return frozenset(effective | floor)


def _org_layer_is_self() -> bool:
    """True when the tenant-scope deny-list belongs to the person using it."""
    try:
        from ..deployment import is_desktop

        return is_desktop()
    except Exception:  # noqa: BLE001 — treat an unknown profile as multi-user
        return False


def is_disabled(name: str, state: Any) -> bool:
    return name in disabled_names(state)


def filter_skills(skills: Iterable[Any], state: Any) -> list[Any]:
    """Drop disabled skills from a list of ADK `Skill` objects."""
    denied = disabled_names(state)
    if not denied:
        return list(skills)
    out = []
    for s in skills:
        try:
            name = s.frontmatter.name
        except Exception:  # noqa: BLE001 — never let a malformed skill hide the rest
            out.append(s)
            continue
        if name not in denied:
            out.append(s)
    return out


def refusal(name: str) -> dict:
    """What a skill tool returns when asked for a disabled skill.

    Enforced at every entry point, not just the catalog: a model that saw the
    name earlier in the conversation (or in a plan, or in its own scratchpad)
    must not be able to route around the toggle.
    """
    return {
        "error": (
            f"Skill '{name}' is disabled. Enable it in Settings → Skills to use it."
        ),
        "error_code": "SKILL_DISABLED",
    }


# --- catalog (for the UI) ---------------------------------------------------

def catalog(
    *,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    scoped_dirs: Iterable[tuple[str, Path]] = (),
) -> list[dict]:
    """Every DISCOVERED skill with its source, enablement, and shadowing.

    `scoped_dirs` carries the sources the process-wide discovery cannot see —
    in the web deployment a user's personal skills and their org's live under
    `<skill_root>/<tenant>/…` and are resolved per request. Pass them in the
    order they shadow (user before org), matching TenantSkillToolset.

    Shadowing is surfaced because it is invisible today and becomes confusing
    the moment toggles exist: a project skill silently replacing a built-in of
    the same name means turning "the built-in" off does nothing visible.
    """
    from .skills import _resolve_skills_dirs, _load_skills_from_dir

    org, user = disabled_for(tenant_id, user_id)
    rows: list[dict] = []
    seen: dict[str, dict] = {}
    sources: list[tuple[str, Path]] = [
        (label, Path(d)) for label, d in scoped_dirs
    ] + [(_classify_source(d), d) for d in _resolve_skills_dirs()]
    for source, base in sources:
        if not base.is_dir():
            continue
        for skill, skill_dir in _load_skills_from_dir(base):
            try:
                name = skill.frontmatter.name
                description = skill.frontmatter.description or ""
            except Exception:  # noqa: BLE001
                continue
            if name in seen:
                seen[name].setdefault("shadows", []).append(
                    {"source": source, "path": str(skill_dir)}
                )
                continue
            row = {
                "name": name,
                "description": description,
                "source": source,
                "path": str(skill_dir),
                "enabled": name not in org and name not in user,
                "disabled_by": ("org" if name in org else "user" if name in user else None),
                "shadows": [],
            }
            seen[name] = row
            rows.append(row)
    rows.sort(key=lambda r: (r["source"], r["name"]))
    return rows


def _classify_source(base: Path) -> str:
    """built-in / global / project / configured — from WHERE the dir was
    resolved, since that is exactly what decides precedence in
    `_resolve_skills_dirs`.

    `global` has to be checked BEFORE the `.adk-cc` test: the run dir's
    `.adk-cc/skills` is global (it applies to every project), and labelling it
    "project" is what made the launch directory look like the user's project in
    the toggle list."""
    builtin = Path(__file__).resolve().parent.parent / "skills"
    try:
        if base.resolve() == builtin.resolve():
            return "built-in"
    except OSError:
        pass
    for g in _global_skill_dirs():
        try:
            if base.resolve() == g.resolve():
                return "global"
        except OSError:
            continue
    if ".adk-cc" in base.parts:
        return "project"
    return "configured"


def _global_skill_dirs() -> list[Path]:
    """The install-scoped locations: the process run dir and the data dir."""
    out: list[Path] = []
    try:
        out.append(Path.cwd() / ".adk-cc" / "skills")
    except OSError:
        pass
    try:
        from .. import deployment

        out.append(Path(deployment.data_dir()) / "skills")
    except Exception:  # noqa: BLE001
        pass
    return out
