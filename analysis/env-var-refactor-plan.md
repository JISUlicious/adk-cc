# Env-var refactor plan

**Date:** 2026-07-19. Based on a full audit of all **206 `ADK_CC_*` vars** (6 parallel
subsystem auditors, every read site traced in source, not comments) + a deeper
verification pass (read-timing, parse sites, DATA_DIR safety, test coupling — see
"Deeper inspection" below). This is a plan — proposals for approval, not yet applied.

## Findings

- **206 distinct env vars, read across 58 files, no central schema.** Every module does
  its own inline `os.environ.get(...)` with a local default. That scatter — not any
  single var — is the root problem: no validation, no discoverability, defaults
  duplicated and occasionally divergent, and the `.env.example` (1110 lines) is
  hand-maintained and drifts from code.
- **The "too many to fill in" pain is a tiering illusion.** Almost everything has a sane
  default. The *actually-required* set is tiny (see below); the rest are advanced tuning,
  per-backend, or web-only vars that a desktop/dev user never touches.
- **Only 1 phantom / dead var**: `ADK_CC_TASK_TITLES` (zero occurrences repo-wide — a
  conflation with `ADK_CC_TOOL_TITLES`). Every other var has a live read site.
- **~30 vars can be removed or merged with zero capability loss;** ~65 more can be
  *demoted* out of the primary surface (per-backend / web-only / advanced sections).

### The genuinely-required set (everything else has a default)

| Var | When required |
|---|---|
| `ADK_CC_API_KEY` | Always (unless the model endpoint is intentionally keyless). The only universal required var. |
| `ADK_CC_DAYTONA_API_URL` + `_API_KEY` | Only if `SANDBOX_BACKEND=daytona`. |
| `ADK_CC_SANDBOX_SERVICE_URL` + `_SHARED_TOKEN` | Only if `SANDBOX_BACKEND=sandbox_service`. |
| `ADK_CC_SSH_HOST` + `_WORKSPACE_PATH` | Only if `SANDBOX_BACKEND=ssh` via the env factory (desktop per-project binding doesn't use these). |
| `ADK_CC_BOOTSTRAP_ADMIN_EMAIL` + `_PASSWORD` | Only on first boot of web password-auth mode. |
| `ADK_CC_AGENTS_DIR` | Currently required by `make_app()` — **but shouldn't be** (it's a code path, trivially package-relative-defaultable; fix below). |

So a desktop/dev user's real fill-in surface is **one variable** (`ADK_CC_API_KEY`), plus
the endpoint/model if not using localhost defaults.

## The refactor — two independent thrusts

### Thrust A — central typed config (fixes the 58-file scatter)

Introduce `agents/adk_cc/config.py`: a single typed schema where each field carries
`name`, `type`, `default`, `tier`, `profile` (all/web/desktop/backend:X), and one-line
help. One `load_config()` reads env **once**, validates (fail-fast on bad combos), and the
rest of the app imports the resolved object instead of calling `os.environ` directly.
Benefits:
- `.env.example` and the docs reference are **generated from the schema** — no more drift.
- `adk-cc config --check` / `--print` validates a deployment and shows effective values.
- Cross-var invariants enforced in one place (e.g. `COMPACTION_TOKEN_THRESHOLD` +
  `EVENT_RETENTION` must co-exist — today the code `raise`s at boot; the schema makes it a
  single field).
- Pydantic is already a transitive dep (ADK/google-genai), so `BaseSettings` is available;
  keep `deployment.py` stdlib-only by having it read the resolved config, not env.

Migration is incremental: land the schema, then convert modules cluster-by-cluster to read
`config.X` (each conversion is behavior-preserving and independently testable). The 58-file
scatter collapses to one reader + thin accessors.

### Thrust B — surface reduction (the deltas below)

Four moves, each reduces what a user sees without losing capability:

**1. Tier every var** → `.env.example` becomes a ~15-line **Quickstart** (required + common)
plus a generated **Reference** (advanced), instead of 1110 undifferentiated lines.

**2. Profile-gate the surface** → hide the entire **web/multi-tenant** group (28 vars: all
`AUTH_*`, `JWT_*`, `AUTHZ`, `ADMIN_*`, `BOOTSTRAP_*`, `TENANCY_MODE`, `GLOBAL_TENANT_ID`,
`TRUST_PROXY`, `IDENTITY_DIR`) when `is_desktop()`; hide **per-backend** groups (Daytona 17,
sandbox_service 9, SSH 6, Docker 4) unless that backend is selected.

**3. One data root** `ADK_CC_DATA_DIR` (rename/promote of `ADMIN_DATA_DIR`) → derive
`identity/`, `registry/`, `skills/`, `credentials/`, `admin-data/`, central-mode `tasks/`,
`audit.jsonl`, codex store. Desktop → `desktop_data_dir()`; web → `<cwd>/.adk-cc`. Keep each
existing `*_DIR` as an override. **Also fixes a real bug**: today `IDENTITY_DIR`
(cwd-relative `./.adk-cc/identity`), the tool audit log (`~/.adk-cc/`), and `ADMIN_DATA_DIR`
(`$CWD/.adk-cc/admin-data`) default to **three different roots by accident**. Excludes
`WORKSPACE_ROOT` and its `.memory`/`.wiki`/workspace-`tasks` derivatives — that data
intentionally travels with the tenant workspace.

**4. Merge/remove families** (below).

## Delta tables (for approval)

### REMOVE (dead / test-only / micro-knob → constant)

| Var | Why |
|---|---|
| `ADK_CC_TASK_TITLES` | **Dead** — phantom, never existed. |
| `ADK_CC_MODEL_DEFAULT_NAME` | Cosmetic registry display name; hardcode "default". |
| `ADK_CC_DAYTONA_CREATE_BACKOFF_BASE_S`, `_CAP_S` | Micro-knobs; `MAX_ATTEMPTS`+`TOTAL_WAIT` already bound the loop. Hardcode 0.5/8. |
| `ADK_CC_MEMORY_CONSOLIDATE_DELAY_S` | Boot-settle test hook; hardcode 60. |
| `ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE`, `_TURNS_BETWEEN`, `_OPEN_TURNS`, `_OPEN_BETWEEN` | 4 cadence ints nobody sets; → `TaskReminderPlugin.__init__` kwargs. |
| `ADK_CC_TASK_REMINDER_DEBUG` | → `_log.debug` gated by `LOG_LEVEL` (also fixes an import-time read). |
| `ADK_CC_WEB_FETCH_PDF_MAX_BYTES` | Single site, never set; → module constant. |

### MERGE (fold into a single var; accept old spelling for one release)

| From → Into | How |
|---|---|
| `MAX_OUTPUT_TOKENS_ESCALATED` → `MAX_OUTPUT_TOKENS` | `"8192,32768"` (base,escalated). |
| `MODEL_MIN_INTERVAL_S` → `MODEL_MAX_RPM` | Same throttle; accept fractional RPM (60/interval). |
| `COMPACTION_EVENT_RETENTION` → `COMPACTION_TOKEN_THRESHOLD` | `"70000,10"` — code *requires* both today. |
| `COMPACTION_BREAKER_COOLDOWN_S` → `COMPACTION_BREAKER_THRESHOLD` (→`COMPACTION_BREAKER`) | `"3,60"`. |
| `COMPACTION_PROMPT_FILE` → `COMPACTION_PROMPT` | `@/path` convention. |
| `COMPACTION_SEED_BUDGET` → `COMPACTION_SEED_MEMORY` | `=<budget>`; 0/unset = off. |
| `MEMORY_STORE_URI` → `MEMORY_ROOT`, `WIKI_STORE_URI` → `WIKI_ROOT` | Only `file://` is implemented; URI is redundant. Keep the code seam. |
| `MEMORY_RESOLVE_VERIFY` → `MEMORY_RESOLVE` | Tri-state `0\|unverified\|1`. |
| `MEMORY_COMPACT` → `MEMORY_SYNTH` | One "no background model calls" switch (also fixes the stale docstring bug). |
| `SANDBOX_CPU_QUOTA` → `SANDBOX_CPUS` | Derive `cpu_quota = cpus*100000` in docker backend. |
| `SANDBOX_MODE` (env) → `SANDBOX_BACKEND` | `MODE=container` ≡ `BACKEND=container`; keep MODE only as the desktop stored-setting. |
| `AUTH_ISSUER` → `JWT_ISSUER`, `AUTH_AUDIENCE` → `JWT_AUDIENCE` | Mint-vs-verify are mutually exclusive; one pair serves both. |
| `AUTH_RATELIMIT` → `AUTH_RATELIMIT_MAX` | `MAX=0` = off. |
| `MCP_SERVER`, `_SERVER_NAME`, `_TRANSPORT`, `_USE_RESOURCES`, `_SAVE_RESOURCES_AS_ARTIFACTS` → `MCP_SERVERS_FILE` | Legacy single-server cluster → one file entry (schema already richer). Synthesize + deprecation-warn for one release. **−5 vars.** |
| `MCP_AUTOSAVE_AUDIENCE_USER_ONLY` → `MCP_AUTOSAVE_EXPORTS` | Enum `0\|user\|all`. |
| `S3_ENDPOINT_URL` → `AWS_ENDPOINT_URL` | Already the fallback; branded duplicate of the ecosystem-standard var. |
| `SKILL_RESOURCE_DEFAULT_LINES` → derive `min(200, MAX_LINES)` | Optional micro-cleanup. |

### RENAME (family/clarity)

| From → To | Why |
|---|---|
| `ADK_CC_CONTEXT_MAX_BYTES` → `ADK_CC_CONTEXT_FILES_MAX_BYTES` | It caps project-context *file* injection, not the token ladder — the current name reads as a fifth `CONTEXT_*_TOKENS` knob. |
| `ADK_CC_ADMIN_DATA_DIR` → `ADK_CC_DATA_DIR` | Promote to the universal data root (Thrust B.3). |
| (optional) `ADK_CC_MAX_CONTEXT_TOKENS` → `ADK_CC_CONTEXT_MAX_TOKENS` | Family consistency with the guard ladder it anchors. |

### DERIVE under `ADK_CC_DATA_DIR` (stop being independently-set; keep as overrides)

`IDENTITY_DIR`, `ADMIN_DATA_DIR`, `CREDENTIAL_STORE_DIR`, `TENANT_REGISTRY_DIR`,
`TENANT_SKILLS_DIR`, central-mode `TASKS_DIR`, `AUDIT_LOG`, `CODEX_STORE_DIR`.

### New defaults (stop being required)

- `ADK_CC_AGENTS_DIR` → default `Path(__file__).parents[2]/"agents"` (like `UI_DIST` already
  does). Removes the sole needless REQUIRED var.
- `ADK_CC_SERVE_UI` → auto-enable when `web/dist/index.html` exists; demote to advanced.

## Code fixes to land while here (found during the audit)

1. **Credential-provider selection triplicated** across `agent.py`, `service/server.py`,
   `credentials/factory.py` — and the agent/server copies reject `"none"` which factory
   accepts. Dedup to `factory.py`.
2. **`memory_scheduler.py` docstring is stale/wrong** — claims "NO model calls" but the
   scheduler runs LLM synthesis + compaction; `MEMORY_SYNTH=deterministic` does *not* stop
   the compaction model call. Fix behavior (fold `MEMORY_COMPACT`→`MEMORY_SYNTH`) + doc.
3. **`secret_redaction.py`** uses `float("")→ValueError→default` as its default idiom —
   replace with a clean parse.
4. **`deployment.noop_ack_host_exec` docstring** says "not yet consulted" — it is
   (`noop_backend.py:103`). Correct it.
5. **Divergent defaults to unify**: `SANDBOX_PIDS_LIMIT` (docker 256 vs container 512);
   `SANDBOX_IMAGE` (differs per backend — document); docker backend hardcodes
   `network=none` so `SANDBOX_NETWORK` is silently ignored there — document.
6. **`MEMORY_STALE_DAYS`** honored by in-process paths but ignored by the external cron
   (`scripts/memory_consolidator.py` uses a flag default) — unify.

## Phased plan

- **P1 — schema + generated docs (no behavior change).** Land `config.py` with the full
  typed schema (tiers/profiles/help) mirroring *current* behavior; generate `.env.example`
  (Quickstart + Reference) and a docs page from it. Add `adk-cc config --check/--print`.
  Immediately kills the 1110-line hand-maintained file and gives the user the tiered view.
- **P2 — removes + renames + new-defaults.** Delete the dead/test-only vars, rename the
  three, default `AGENTS_DIR`/`SERVE_UI`. Low risk; each is local.
- **P3 — `ADK_CC_DATA_DIR` consolidation** + the code fixes (1,4,5). Behavior-preserving
  (every old `*_DIR` stays an override); fixes the three-accidental-roots bug.
- **P4 — REVISED to robustness-first (2026-07-20).** The original P4 was "merges" (fold
  vars, reduce count). User pushback: *"Is merging needed? It may fall into complexity of
  guessing what merged values are or missing one… the purpose is not just to reduce env
  vars, but making it reasonable and robust."* Re-scoped accordingly:
  - **Dropped** all positional-comma merges (`"8192,32768"`) and overloaded-bool merges —
    they create value-guessing / missing-one ambiguity, the opposite of robust.
  - **Shipped (commit e6aa85f) — self-validating config:** `Var.choices` on 9 enum vars
    (`check` errors on an out-of-choice value); a `Rule` layer of 6 cross-var checks
    (compaction pair [error]; JWKS-without-iss/aud, both-auth-modes, allowlist-ignored,
    SSRF-off, no-auth-in-web [warns]); `make_app` runs the check at boot (web **and**
    desktop, same factory), logging specific messages. Misconfig now surfaces loudly/early
    instead of silently or as a late crash.
  - **Shipped (commit 40a5206) — unify divergent handling:** credential-provider selection
    (was re-implemented in 3 places, diverging on `none` + store-dir defaulting → a
    documented value crashed 2 paths) now delegates to the one factory; `SANDBOX_PIDS_LIMIT`
    unified to 512 (docker was 256); `MEMORY_SYNTH` docs corrected (it governs only
    consolidation synthesis, not the whole hot path).
  - **Shipped (commit a7de657):** guard comment on `context_guard`'s `gpt-4` tokenizer
    fallback so a divergent-default sweep doesn't "unify" it into a bug.
  - **Divergent-default sweep:** scanned all multi-site inline defaults; after the PIDS fix
    the only remaining split is the intentional `gpt-4` tokenizer fallback. Clean.
  - **Removals re-evaluated — mostly REJECTED:** on inspection `MEMORY_STORE_URI`,
    `WIKI_STORE_URI` (docstore backend selection), `S3_ENDPOINT_URL` (S3-compat endpoints),
    `SANDBOX_CPU_QUOTA`, `COMPACTION_SEED_BUDGET`, and `SANDBOX_MODE` (desktop host/container
    preference w/ stored-setting fallback) are **functional knobs, not dead vars** — removing
    them trades capability for a lower count, which the robustness framing rejects. Only
    `MODEL_MIN_INTERVAL_S` is a genuine duplicate (fully subsumed by `MODEL_MAX_RPM`, clear
    precedence), and it's harmless + already documented as "alternative to MAX_RPM" — left as-is.
- **P5 — deprecation removal** (next major): the MCP single-server → `SERVERS_FILE`
  consolidation remains deferred. Drop the `ADK_CC_CONTEXT_MAX_BYTES` alias (DEPRECATED
  registry) then too.

## Post-merge review + fixes (2026-07-21/22)

A 10-angle adversarial review of the merged series (`0fe75a1..5b56cbf`) confirmed 36
findings. Root cause of most: the schema was a hand-maintained mirror asserting semantics
the runtime contradicted. Fixed in four commits (70967cd, a752094, b9341bd, cc7718d):

- **One boolean convention** (user-approved behavior change): `config.schema.env_bool`
  replaces the 3–4 incompatible read-site conventions at ~49 sites/31 files. The schema
  is now the SOURCE OF TRUTH for booleans (not a mirror). Fixed the inverted diagnostics
  (ALLOW_NO_AUTH=true warned "no auth" while actually failing closed; DESKTOP=true made
  check validate the wrong profile; AUTH_PASSWORD=true demanded bootstrap creds while
  password auth stayed off).
- **Data root back to home** (user-approved): `data_dir()` web fallback `~/.adk-cc`
  (cwd-relative briefly shipped and silently relocated audit/tasks/codex data); codex
  regained the legacy `ADK_CC_DESKTOP_DATA` fallback; admin store warn-and-uses its
  legacy cwd location; encrypted-secret store dir is conditionally-required + logged
  when defaulted; AGENTS_DIR raises again in the pip-installed layout.
- **Trustworthy check**: moved to `adk_cc/__init__` (covers `adk web`/desktop/uvicorn,
  runs before the agent graph); validator failures WARN not DEBUG; API_KEY missing is a
  warning (keyless boot is supported — `Var.hard=False`); compaction rule mirrors
  agent.py's real gate incl. INTERVAL; AUTH_TOKENS-ignored rule added; resolve()
  collapses bad enums to defaults; as_csv is comma-only (host:port safe) with
  as_csv_colon for the PATH-style permission lists; SANDBOX_IMAGE documents its real
  per-backend defaults.
- **Deprecation registry**: REMOVED/DEPRECATED dicts in the schema — retired names warn
  at boot; CONTEXT_MAX_BYTES honored again as a deprecated alias; stale README/docs
  cadence + pids_limit=256 references fixed; generator no longer emits `=unset`/
  tag-in-value garbage (metadata on the comment line, value side = example or empty).
- **Guard widened**: defaults lock 19→46 vars (incl. the two whose divergence started
  this); secret-flag test forces a masking decision on credential-suffixed vars; Rule
  is (level, message, positive-predicate); _validate_schema rejects plain-string
  tier/profile and defaults outside choices.

Accepted (not fixed): check() re-parses set vars once for warning detection (bounded
cost); bare-assert test style vacuous under `python -O` (nothing runs -O); `adk_cc`
package import still builds the agent graph eagerly (config CLI is heavier than the
schema alone — import `adk_cc.config.schema` directly for stdlib-only use).

## Deeper inspection — verified design & firm decisions (2026-07-19)

Everything below was checked against source before finalizing.

### 1. Central config is feasible — read-timing verified
- Only **~5 import-time env reads** exist in the whole package (`agent.py`
  `_BOOT_MODEL_ID`/`_BOOT_API_BASE`/`_permissions_yaml`; `task_reminder._DEBUG`;
  `chatgpt_codex._BASE_URL`). The other ~269 of 274 reads are **inside functions**
  (runtime) — friendly to a central config.
- The one heavy import-time coupling is `agent.py` building the entire agent graph at
  module import (dozens of reads). But that import is triggered by
  `adk_cc/__init__.py:77` `from . import agent`, which runs **after**
  `_bootstrap_dotenv()`. Both entry paths (`adk web`/`adk run` and the
  `uvicorn …server:make_app --factory`) go through the package `__init__`.
- **Design:** add `load_config()` in `__init__.py` right after `_bootstrap_dotenv()`,
  before the agent import. It resolves a frozen `CONFIG` singleton from `os.environ`
  once. `deployment.py` stays the low-level stdlib mode reader (config imports
  deployment for profile selection; deployment never imports config → no cycle).

### 2. `config.py` shape
- A list of field descriptors — `Field(name, type, default, tier, profile, parse, help)`
  — plus `resolve(environ) -> Config` (pure, unit-testable with a dict) and a `CONFIG`
  global set by `load()`. The tier/profile/help metadata is the point: **`.env.example`
  (Quickstart + Reference) and the docs page are generated from the descriptors** — one
  source of truth, no drift. `adk-cc config --check` validates a live env; `--print`
  shows effective values and which were defaulted. (Custom descriptors are recommended
  over pydantic-settings because the tier/profile/doc metadata drives generation;
  pydantic Field metadata would also work if preferred.)

### 3. Migration = faithful-mirror first, then convert per cluster
- **P1 lands `config.py` mirroring CURRENT defaults exactly**, used only for doc-gen +
  `--check` at first (read-only reporting) — so it cannot change behavior even while
  modules still read `os.environ` directly. Add a test asserting `resolve(clean_env)`
  reproduces the values today's inline code yields.
- Per cluster (P4), flip a module to read `config.X` **and delete its inline read in the
  same PR** — no divergence window. The 274 scattered reads collapse gradually.

### 4. Merge shims — concrete and test-safe (parse sites verified)
Every merge ships a back-compat shim: read the NEW combined var; if unset, fall back to
the OLD var(s). Existing tests/deploys keep working for one release; migrate them in P4.
- **Output tokens:** `resolve_max_output_tokens` (base) + `escalated_max_output_tokens`
  read two vars via `_cap_from`. Merge `ADK_CC_MAX_OUTPUT_TOKENS="8192,32768"`: split on
  comma → field0 base, field1 escalated; shim reads `_ESCALATED` when there's no field1.
- **Throttle:** `_model_min_interval()` already reads `MAX_RPM` (float, `60/v`) then falls
  back to `MIN_INTERVAL_S`. Merge = keep `MAX_RPM`, drop the `MIN_INTERVAL_S` fallback
  after one release.
- **Compaction:** `_make_compaction_config()` reads `THRESHOLD`+`RETENTION` and **`raise`s
  unless both are set**. Merge `ADK_CC_COMPACTION_TOKEN_THRESHOLD="70000,10"`; shim reads
  the two old vars when the combined form has one field. **`RETENTION` is set in 8 test
  files** — the shim is what keeps them green (verified).
- Test coupling checked for every remove candidate: **0 test/script refs** for
  `MODEL_DEFAULT_NAME`, the Daytona backoff pair, all 4 `TASK_REMINDER_*`,
  `TASK_REMINDER_DEBUG`, `WEB_FETCH_PDF_MAX_BYTES`, `SANDBOX_CPU_QUOTA`, `AUTH_ISSUER`,
  `S3_ENDPOINT_URL` → removes are clean.

### 5. `ADK_CC_DATA_DIR` — verified safe, with one nuance
- The pattern already exists: `_prepare_admin_env()` (`server.py:470-494`) `setdefault`s
  `TENANT_REGISTRY_DIR`/`TENANT_SKILLS_DIR`/`MODEL_REGISTRY_FILE` under `ADMIN_DATA_DIR` —
  just gated on admin-panel. Generalize to run always with a per-profile root.
- Three-accidental-roots bug confirmed: `IDENTITY_DIR` → cwd-relative `.adk-cc/identity`
  (unresolved `os.path.join`); legacy `TASKS_DIR` → `~/.adk-cc/tasks`; `ADMIN_DATA_DIR` →
  `$CWD/.adk-cc/admin-data`. `DATA_DIR` unifies them.
- **Nuance to preserve (the one correctness trap):** `DATA_DIR` sets the ROOT; it must
  NOT unconditionally materialize per-tenant dirs. Feature enablement still gates whether
  a derived dir is *used* (tenant registry/skills only under admin-panel/multi-tenant;
  identity dir only when IdentityService is built). Otherwise plain web mode would start
  creating tenant dirs it never uses. Default the root; don't blanket-mkdir.
- Desktop already threads explicit dirs (`main.rs` sets `DESKTOP_DATA` + secrets/skills/
  wiki/memory); those become overrides and already match the derived shape → behavior-
  preserving.

### 6. Web-vs-desktop split — confirmed from the launcher
`src-tauri/src/main.rs` hard-sets `DESKTOP=1`, `ALLOW_NO_AUTH=1`, `SERVE_UI`/`UI_DIST`,
`CREDENTIAL_*`(encrypted_file), `TENANT_SKILLS_DIR`, `WIKI`/`MEMORY`, `SANDBOX_BACKEND=noop`,
`AGENTS_DIR` — and sets **none** of `AUTH_*`/`JWT_*`/`AUTHZ`/`ADMIN_PANEL`/`BOOTSTRAP_*`,
confirming those 28 are inert in desktop. (It defensively sets `TENANCY_MODE`/
`GLOBAL_TENANT_ID`, both no-ops in desktop — removable.) Since `AGENTS_DIR` is launcher-set
and web-factory-required, a package-relative default removes the requirement for everyone
else while the desktop set still wins.

### Firm decisions on the previously-open items
- **`AGENTS_DIR`** → package-relative default (required → common). Safe: 0 test coupling,
  desktop overrides.
- **`AUTH_MODE`** → ADD `ADK_CC_AUTH_MODE=jwks|password|tokens|none` as the explicit
  selector; the individual vars become mode config; infer from presence for one release.
  Replaces today's silent presence-priority (where `AUTH_PASSWORD=1` alongside `JWKS_URL`
  is silently ignored).
- **`SKILL_GUARDS` default → ON** — recommended, but it's a real behavior change
  (untrusted-content wrapping + host-exec refusal), so ship it as its **own reviewed
  flag-flip**, not silently inside the mechanical refactor. The dev escape hatch
  (`SKILL_SCRIPTS_ACK_HOST_EXEC`) already exists.
- **`MAX_CONTEXT_TOKENS` rename** → SKIP (low value, churns a name). Keep the
  `CONTEXT_MAX_BYTES → CONTEXT_FILES_MAX_BYTES` rename (that one is actively misleading).
- **`SANDBOX_PIDS_LIMIT`** → unify default to 512 across both container backends.

### Risk register
- **Import ordering** — `load_config()` pinned between the two existing `__init__.py`
  statements; both entry paths go through the package `__init__`. Low risk.
- **Faithful-mirror correctness** — P1 schema defaults must byte-match current inline
  defaults; guarded by the `resolve(clean_env)` assertion test.
- **Shim window** — old spellings honored for one release; a boot `DeprecationWarning`
  lists any that are set; removal is P5 (next major).
- **DATA_DIR feature-gate nuance** — gate dir *use*, not just default the root (§5).
- **Test-coupled merges** — `COMPACTION_EVENT_RETENTION` (×8) and a few (×1) rely on the
  shim to stay green; migrate in P4.

## Net effect

- Vars eliminated or merged away: **~30** (no capability lost).
- Primary fill-in surface: **206 → ~1 required** (`API_KEY`) + a ~15-line common Quickstart;
  everything else moves to a generated, profile-sectioned Reference.
- `.env.example`: **1110 hand-maintained lines → generated**, always in sync with code.
- Reads: **58 scattered files → one schema + accessors**, validated at boot.

Full per-var classification (read sites, defaults, tier, verdict, rationale) for all 206 is
in the six audit transcripts under this session's tasks/ dir — this plan is the actionable
synthesis.
