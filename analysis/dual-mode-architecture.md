# Dual-Mode Architecture — adk-cc as web app *and* desktop app

Reframe: adk-cc is **one multi-tenant web core** with **desktop as a deployment
profile**, not two products. The design goal is that "web app" and "desktop app"
are two *profiles* of the same codebase, with the mode-specific differences
forked at a small set of clean seams — and the agent/tool core mode-agnostic.

## 1. What actually varies between the two modes

Everything else is shared. Only a thin "platform ring" changes:

| Axis | Web (multi-tenant) | Desktop (local single-user) | Current seam |
|---|---|---|---|
| Identity / auth | JWT/token, per-user | no-auth, fixed `local` user | `auth_extractor` in `make_app` |
| Workspace | **isolated** per tenant/session | **in-place** on the project *(should be)* | `_make_tenancy_plugin` → tenant resolver |
| Sandbox / exec | `daytona` (remote) | `noop` / OS-sandbox (local) | `ADK_CC_SANDBOX_BACKEND`, seeded into tenant ctx |
| Session store | Postgres DSN | local sqlite | `ADK_CC_SESSION_DSN` |
| Credential store | tenant/user registry | encrypted-file (local) | credential provider env |
| Project model | user → sessions | **project (repo) → session → workspace** | `desktop_routes` (mounted when desktop) |
| Frontend shell | login + artifacts panel | projects rail + file-tree panel | build-time `VITE_ADK_CC_DESKTOP` + WebApp/DesktopApp |
| Undo / safety | isolation *is* the safety | **checkpoint/rewind** *(missing)* | — |

## 2. The good news — the core is already mode-agnostic

The agent loop, tools, permission engine, and ADK plugins **do not know the
mode**. Mode lives in the platform ring, and the seams are mostly clean:

- **One workspace fork.** `_make_tenancy_plugin()` picks the tenant resolver
  (desktop → `desktop_tenant_resolver`; else standard). `TenancyPlugin` seeds
  `state["temp:tenant_context"] / sandbox_workspace / sandbox_backend` before any
  tool runs, so **tools consume an injected workspace + sandbox** — they never
  branch on mode.
- **Desktop code is isolated.** `service/desktop_*` modules mount only when
  `desktop_enabled()`; they don't leak into the shared path.
- **Frontend composes per shell.** `WebApp` / `DesktopApp` inject different
  `Rail`/`Settings`/`RightPanel` into one `ChatPage`.

That's a solid base. adk-cc is genuinely dual-purpose by design, not bolted-on.

## 3. The architectural debt (what makes it feel less clean)

1. **The mode signal is fragmented.** "Desktop" isn't one decision — it's ~10
   env vars (`ADK_CC_DESKTOP`, `_DESKTOP_DATA`, `TENANCY_MODE`, `SANDBOX_BACKEND`,
   `SESSION_DSN`, `SERVE_UI`, `UI_DIST`, `CREDENTIAL_PROVIDER`, `ALLOW_NO_AUTH`,
   `GLOBAL_TENANT_ID`) that must be set *coherently*. `main.rs` sets them for
   desktop; the operator sets them for web. There is no single object that says
   "this is the desktop profile," so coherence is caller-enforced and
   "what desktop means" is spread across files.
2. **Workspace strategy is hardcoded, not chosen.** Desktop *always* makes a
   worktree; there's no "in-place vs isolated" strategy. That's why the in-place
   change felt like surgery instead of a policy swap.
3. **No undo policy.** Multi-tenant gets safety from isolation; desktop in-place
   would need checkpoint/rewind, and there's no abstraction for it.

## 4. Proposed clean design

### 4a. One `DeploymentProfile`, resolved once
Introduce a single profile concept (`web` | `desktop`) resolved from **one**
signal (`ADK_CC_MODE`, or keep `ADK_CC_DESKTOP` as the trigger). It produces a
coherent bundle of *policy objects*:

```
Profile.desktop = {
  auth:        NoAuth(user="local"),
  workspace:   InPlaceWorkspace(),          # project root, opt-in isolate
  sandbox:     LocalSandbox(),              # OS-sandbox (was noop)
  sessions:    SqliteStore(data_dir),
  credentials: EncryptedFileStore(data_dir),
  routes:      [desktop_projects, desktop_files, desktop_settings],
  shell:       "desktop",                   # served dist-desktop
  undo:        CheckpointRewind(),
}
Profile.web = {
  auth: JwtOrToken(), workspace: IsolatedWorkspace(), sandbox: RemoteSandbox(daytona),
  sessions: DbStore(dsn), credentials: RegistryStore(), routes: [tenant, org, admin],
  shell: "web", undo: IsolationIsUndo(),
}
```

Each seam **consumes the profile** instead of independently reading env vars.
"What desktop is" lives in one place; incoherent combos become impossible. This
*is* your "fork by code path at a clean seam, don't scatter flags" principle —
it **consolidates** the ten flags into one fork. (Env vars can still override
individual policies for advanced/hybrid deployments, but the *default bundle*
comes from the profile.)

### 4b. `WorkspaceStrategy` as a first-class policy
The tenant resolver already exists; make the *strategy* explicit:
- `IsolatedWorkspace` (web/multi): per-tenant/session worktree or remote sandbox.
- `InPlaceWorkspace` (desktop): return the **project root** directly; keep
  worktree as an **opt-in** ("isolate this session") and for parallel subagents —
  exactly how Claude Code and Hermes scope worktrees.

Same seam (`DesktopTenantContext.workspace()`), swapped implementation.

### 4c. `Undo` policy
- Multi-tenant: isolation is the safety net (no change).
- Desktop in-place: a `CheckpointRewind` (shadow-git snapshot of the workspace
  before mutating tools, once per turn) — the thing that makes in-place safe.

### 4d. Core stays mode-agnostic (no change)
Agent loop, tools, permission engine keep consuming injected workspace/sandbox.

## 5. "Both at the same time" — what it can and can't mean

- ✅ **Same codebase, build once, run as either profile** (mode chosen at
  startup). This is the achievable, clean goal.
- ⚠️ **One process serving both simultaneously** is *not* coherent: the workspace
  and auth policies are per-*deployment*, not per-request — a request can't be
  "in-place single-user" and "isolated multi-tenant" at once. The frontend is
  also a per-mode build (`dist` vs `dist-desktop`) served by the matching
  backend profile.
- The realistic "both" is: the **web service** for teams, and the **desktop app**
  for a single user's laptop — the same code, two profiles. (A hybrid "personal
  cloud" — your own hosted single-user instance — is just the desktop profile
  with a DB session store + auth in front; the profile model expresses that
  cleanly.)

## 6. Migration (incremental, low-risk)

1. **Introduce the `Profile` resolver** wrapping today's env vars — *no behavior
   change*; just one place that defines the desktop/web bundles.
2. **Make `WorkspaceStrategy` explicit**; flip desktop to **in-place** (the P0
   from the workspace gap analysis) — a strategy swap, not scattered edits.
3. **Add `CheckpointRewind`** for in-place safety.
4. *(optional)* collapse the env sprawl behind `ADK_CC_MODE` + profile defaults,
   keeping per-policy overrides for advanced use.

Net: the core is already right; the win is **naming the profile** (one coherent
fork instead of ten flags) and **elevating workspace + undo to policies**, which
also lands the in-place desktop behavior cleanly.
