# Plan — Per-user MCP servers & skills (alongside tenant-managed)

Status: in progress · Date: 2026-06-26
Related: [skills-env-management-plan.md](./skills-env-management-plan.md)

## Goal

Separate **tenant-managed** and **user-managed** MCP servers / skills; a user's
agent uses the **union** of both. Mirrors the user-over-tenant credential model
already shipped — same `_users/<user>` storage layout, same precedence.

## Scopes (per resource type)

| Scope | Skills root | MCP source | Managed by |
|---|---|---|---|
| Operator/global | `ADK_CC_SKILLS_DIR` / bundled | `ADK_CC_MCP_SERVERS_FILE` | deployment |
| Tenant | `<TENANT_SKILLS_DIR>/<tenant>/` | registry `<root>/<tenant>/mcp.json` | org admin (exists) |
| **User (new)** | `<TENANT_SKILLS_DIR>/<tenant>/_users/<user>/` | `<root>/<tenant>/_users/<user>/mcp.json` | the user (self-service) |

**Effective set = union**, deduped by name with **user > tenant > global**
(a personal `pdf-tools` shadows the org's; a personal MCP `github` shadows the
org's, keeping the `mcp__github__` prefix unambiguous). "User can use both" =
this union. `user_id=None` everywhere keeps the single-tenant/dev path flat.

## Phases (each committed)

### Phase 1 — (tenant,user)-aware declaration discovery  ← folds in the gap
`credentials/required_inputs.py`: `discover_skill_required_inputs(tenant_id,
user_id)` scans global dirs **+** `<TENANT_SKILLS_DIR>/<tenant>/` **+**
`<TENANT_SKILLS_DIR>/<tenant>/_users/<user>/`. `discover_groups` /
`declared_secret_keys` gain `(tenant_id, user_id)`. Replace the
process-lifetime `_CACHE` with a short per-(tenant,user) TTL (hot-reload like
the toolsets). Thread `user_id` at the `/auth/secrets` route and
`make_default_backend` call sites. → the Secrets grouping becomes correct for
tenant AND user skills.

### Phase 2 — TenantSkillToolset unions tenant + user skills
`tools/skills_tenant.py`: also scan `<root>/<tenant>/_users/<user>/`; union with
the tenant dir, dedup by skill name (user wins). Reads `user_id` from
`temp:tenant_context`.

### Phase 3 — Registry user dimension + TenantMcpToolset union
`service/registry.py`: `_path(tenant, user_id=None)` →
`<root>/<tenant>/_users/<user>/<kind>.json`; `list_for_tenant_user(tenant,
user)` = tenant ∪ user (dedup by id, user wins); `add/remove(..., user_id=None)`.
`tools/mcp_tenant.py`: resolve the union (already reads `user_id`).

### Phase 4 — Self-service APIs (user-scoped, no admin role)
`service/identity_routes.py`, mirroring the admin tabs but for the caller:
- `GET/PUT/DELETE /auth/mcp-servers[/{name}]`
- `GET/PUT/DELETE /auth/skills[/{name}]` (zip upload; reuse the admin extract
  helper). Per-user count/size limits.

### Phase 5 — Account-page UI
Two sections (siblings of Secrets): **Your MCP servers** (add/remove: name,
transport, url, credential_key) and **Your skills** (upload .zip / remove). The
existing Secrets grouping then shows env-var groups for personal + org MCP/skills
automatically (Phase 1 discovery).

## Security / decisions
- Isolation by `_safe_component` on tenant_id, user_id, name (path traversal).
- A personal skill = the user's own code in the user's own sandbox; a personal
  MCP = the user's own external service + their own token (set in their personal
  credential scope, resolved user-over-tenant). Both confined to that user.
- Dedup by name, **user wins** (not prefix-namespaced) — unambiguous tool names.
- Upload limits per user (size, count) like the admin path.

## Verification
- Unit: discovery union + precedence (user shadows tenant shadows global);
  registry user dimension (scope-exact writes, union read); traversal safety.
- HTTP e2e: user adds an MCP/skill → appears for them, not for another user;
  tenant resource still visible; user override shadows tenant by name.
- Real-browser e2e: the two Account sections add/remove; Secrets grouping shows
  the personal MCP/skill's env var with a needs-setup badge.
