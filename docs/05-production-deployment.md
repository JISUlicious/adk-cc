# Production deployment

This is the runbook + readiness checklist for taking adk-cc from `adk web .` on a laptop to a multi-tenant FastAPI service. Read end-to-end before standing up production; the order matters.

> **Status: alpha.** adk-cc is functional and exercised end-to-end (`tests/e2e_features.py`) but has not yet been hardened against the operational shocks of a real deployment. The checklist below honestly marks what works (✓), what's partial (⚠️), and what's missing (✗). Operators should close the ✗ items appropriate to their threat model and SLO before serving real users.

## Topology

```
                                                       ┌──────────────┐
            ┌────────────────────────────┐             │   IdP        │
            │  K8s cluster               │             │  (issues     │
            │                            │ JWKS fetch  │   JWTs)      │
            │   ┌────────────────────┐   │◄──HTTPS─────┤              │
            │   │  adk-cc agent pod  │   │             └──────────────┘
            │   │  - JwtAuthMW       │   │
            │   │  - FastAPI factory │   │ Docker mTLS ┌──────────────┐
            │   │  - DockerBackend ──┼───┼─port 2376───►  Sandbox VM  │
            │   └────┬───────────────┘   │             │  (per-       │
            │        │                   │             │   session    │
            └────────┼───────────────────┘             │   containers)│
                     │ TCP 5432                        └──────────────┘
                     ▼
            ┌─────────────────────┐
            │  Postgres           │
            │  (ADK sessions)     │
            └─────────────────────┘
```

Five external dependencies the agent pod relies on:

1. **IdP** issuing JWTs the agent will accept. Provides JWKS at a stable URL.
2. **Postgres** for ADK session storage. Single instance is fine for a few hundred users; sized per ADK's session schema.
3. **Sandbox VM** running Docker daemon, accepting mTLS connections from the agent pod only. See [`04-deployment-sandbox.md`](./04-deployment-sandbox.md).
4. **Persistent volume** for the agent pod's tasks / credentials / tenant registry / audit log. Workspaces live on the sandbox VM, not here.
5. **Model server** (LLM) the agent talks to via `LiteLlm`. Could be a hosted Anthropic / OpenAI endpoint or a self-hosted vLLM / mlx_lm.

## Step-by-step deployment

### 1. Sandbox VM (one-time)

Follow [`04-deployment-sandbox.md`](./04-deployment-sandbox.md) to provision the Linux VM, install Docker, build `adk-cc-sandbox:latest`, configure mTLS, generate the cert pair. Note the VM's hostname and the path you choose for `/var/lib/adk-cc/wks`.

### 2. Postgres

```sql
CREATE DATABASE adk_cc;
CREATE USER adk_cc WITH PASSWORD '<pick>';
GRANT ALL PRIVILEGES ON DATABASE adk_cc TO adk_cc;
```

ADK creates its session schema on first use. The DSN goes into `ADK_CC_SESSION_DSN`.

### 3. Generate the Fernet credential key (one-time)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store this in your secret manager. Loss = inability to decrypt any registered credential. Compromise = full credential disclosure across all tenants.

### 4. Wire JWT validation

Three required env vars:

```
ADK_CC_JWT_JWKS_URL=https://idp.example.com/.well-known/jwks.json
ADK_CC_JWT_ISSUER=https://idp.example.com
ADK_CC_JWT_AUDIENCE=adk-cc
```

Optional (defaults shown):

```
ADK_CC_JWT_USER_CLAIM=sub        # which JWT claim is the user id
ADK_CC_JWT_TENANT_CLAIM=tenant   # which JWT claim is the tenant id
```

The IdP must include both claims in tokens it issues to clients. If your IdP uses different claim names, override above. If your IdP doesn't include a tenant claim, **don't deploy yet** — implement a custom `AuthExtractor` (see "Custom auth" below).

### 5. Full env config

Start from [`../.env.example`](../.env.example). Minimum production set:

```bash
# Model
ADK_CC_API_KEY=...
ADK_CC_API_BASE=https://your-llm-host/v1
ADK_CC_MODEL=...

# Service
ADK_CC_AGENTS_DIR=/srv/adk-cc           # parent of adk_cc/
ADK_CC_SESSION_DSN=postgresql://adk_cc:...@postgres:5432/adk_cc
ADK_CC_PERMISSION_MODE=default

# Auth (production)
ADK_CC_JWT_JWKS_URL=...
ADK_CC_JWT_ISSUER=...
ADK_CC_JWT_AUDIENCE=adk-cc

# Sandbox
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2376
ADK_CC_DOCKER_CA_CERT=/etc/adk-cc/docker-tls/ca.pem
ADK_CC_DOCKER_CLIENT_CERT=/etc/adk-cc/docker-tls/cert.pem
ADK_CC_DOCKER_CLIENT_KEY=/etc/adk-cc/docker-tls/key.pem
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks   # path on the SANDBOX VM

# Per-tenant resources (multi-tenant SaaS)
ADK_CC_TENANT_REGISTRY_DIR=/var/lib/adk-cc/tenants
ADK_CC_CREDENTIAL_PROVIDER=encrypted_file
ADK_CC_CREDENTIAL_KEY=<paste-fernet-key>
ADK_CC_CREDENTIAL_STORE_DIR=/var/lib/adk-cc/credentials
ADK_CC_TENANT_SKILLS_DIR=/var/lib/adk-cc/skills

# Tasks / audit
ADK_CC_TASKS_DIR=/var/lib/adk-cc/tasks
ADK_CC_AUDIT_LOG=/var/log/adk-cc/audit.jsonl

# Context guardrail (recommended for production)
ADK_CC_MAX_CONTEXT_TOKENS=100000          # main model's window
ADK_CC_COMPACTION_TOKEN_THRESHOLD=70000   # ADK compacts past this
ADK_CC_COMPACTION_EVENT_RETENTION=10      # keep last N raw events
# ADK_CC_COMPACTION_MODEL=openai/gpt-4o-mini   # optional cheaper compaction model
```

Mount `/var/lib/adk-cc/{tasks,credentials,tenants,skills}` and `/var/log/adk-cc/` from a persistent volume; pod restarts otherwise lose state.

### 6. Run

```bash
uvicorn adk_cc.service.server:make_app --factory \
  --host 0.0.0.0 --port 8000 --workers 4
```

`make_app` fails closed if `ADK_CC_JWT_JWKS_URL` and `ADK_CC_AUTH_TOKENS` are both unset (unless `ADK_CC_ALLOW_NO_AUTH=1`). Pin to JWT for production.

### 7. (Optional) Mount admin routes for tenant self-serve

`make_app` does NOT mount admin routes by default. If your tenants will self-serve credential/MCP/skill registration over HTTP, write a thin wrapper:

```python
# my_factory.py
import os
from adk_cc.service.server import build_fastapi_app
from adk_cc.service.admin_routes import mount_tenant_admin
from adk_cc.credentials import EncryptedFileCredentialProvider
from adk_cc.service.registry import JsonFileTenantResourceRegistry
from adk_cc.tools.mcp_tenant import McpServerConfig
from adk_cc.service.auth import JwtAuthExtractor

def app():
    extractor = JwtAuthExtractor(
        jwks_url=os.environ["ADK_CC_JWT_JWKS_URL"],
        issuer=os.environ["ADK_CC_JWT_ISSUER"],
        audience=os.environ["ADK_CC_JWT_AUDIENCE"],
    )
    creds = EncryptedFileCredentialProvider(
        root=os.environ["ADK_CC_CREDENTIAL_STORE_DIR"],
    )
    registry = JsonFileTenantResourceRegistry[McpServerConfig](
        root=os.environ["ADK_CC_TENANT_REGISTRY_DIR"],
        kind="mcp", model=McpServerConfig, id_attr="server_name",
    )
    fastapi_app = build_fastapi_app(
        agents_dir=os.environ["ADK_CC_AGENTS_DIR"],
        session_service_uri=os.environ.get("ADK_CC_SESSION_DSN"),
        auth_extractor=extractor,
        # ... other build_fastapi_app args
    )
    mount_tenant_admin(
        fastapi_app, registry=registry, credentials=creds,
        skill_root=os.environ.get("ADK_CC_TENANT_SKILLS_DIR"),
    )
    return fastapi_app
```

Then `uvicorn my_factory:app --factory ...`. The default RBAC in `mount_tenant_admin` is "caller's tenant must equal target tenant"; pass `admin_extractor=` for global-admin patterns.

### 8. Smoke test

```bash
# Unauthenticated → 401
curl -i https://your-host/tenants/tenantA/mcp-servers
# Expect: HTTP/1.1 401

# Valid JWT → 200
curl -i https://your-host/tenants/tenantA/mcp-servers \
  -H "Authorization: Bearer $JWT"
# Expect: 200 with empty servers list
```

Drive a session through the live agent (replace placeholders):

```bash
SESSION=test-$(date +%s)
curl -X POST https://your-host/apps/adk_cc/users/alice/sessions/$SESSION \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{}'

curl -X POST https://your-host/run \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d "{\"appName\":\"adk_cc\",\"userId\":\"alice\",\"sessionId\":\"$SESSION\",
       \"newMessage\":{\"role\":\"user\",\"parts\":[{\"text\":\"echo hello\"}]}}"
```

Check on the sandbox VM that `docker ps --filter label=adk-cc-session` shows a per-session container; verify it disappears after the session ends.

## Custom auth

`make_app` ships two stock extractors (JWT, dev BearerToken). For anything else (mTLS client certs, session DB, OAuth introspection), implement the `AuthExtractor` protocol:

```python
class MyAuthExtractor:
    async def __call__(self, request) -> tuple[str, str]:
        # ... your logic, return (user_id, tenant_id) or raise HTTPException
```

Then build the app yourself via `build_fastapi_app(auth_extractor=...)` and skip `make_app` entirely.

## Workspace storage tiers

Workspaces in production are scoped per `<tenant>/<user>/` under `ADK_CC_WORKSPACE_ROOT`. Three operational tiers based on user count; pick one at deploy time. The path layout (`<root>/<tenant>/<user>/`) is identical across all three — Tier 2 / 3 differ only in where storage physically lives.

### Tier 1 — single sandbox VM (≤ ~500 users)

Local NVMe on the sandbox VM. `<root>` is a directory on the VM (e.g. `/var/lib/adk-cc/wks`). Per-session containers bind-mount `<user_home>` directly off local disk.

- **Disk sizing**: ~5 GB per user × 500 = 2.5 TB. Fits on one NVMe.
- **Backup**: ZFS / btrfs / LVM snapshot to off-host (`zfs send`, `restic` to S3-compatible). Per-user dir is the natural unit.
- **Concurrency cap**: ~10 concurrent sessions × 4 GB = 40 GB peak. Past 10, the quota plugin (`ADK_CC_QUOTA_PER_MINUTE`) refuses with a friendly message.
- **GDPR delete**: `rm -rf <root>/<tenant>/<user>/`.
- **Tenant offboarding**: `rm -rf <root>/<tenant>/`.

### Tier 2 — multi-VM with shared FS (~500–5K users) — path-compatible, not implemented

NFS / EFS / Azure Files mounted at `<root>` on every sandbox VM. Same `<root>/<tenant>/<user>/` paths. Quotas at the FS level (NFS quotas) for per-tenant disk caps.

- **Tradeoff**: NFS I/O is 3–10× slower than local NVMe. Fine for code edits + plan files; painful for large data ingest. Mitigate by keeping the per-user `.cache/` on local NVMe (rebuilt lazily from the network FS).
- **Failure mode**: NFS server is a single point of failure. Use a managed service (EFS / Filestore) with replication.

### Tier 3 — VM-affinity sharded (≥ ~5K users) — path-compatible, not implemented

Each sandbox VM owns a slice of users by consistent hash on `user_id`. Workspaces live on the assigned VM's local NVMe. Cross-VM access is a control-plane operation (drain → rsync → swap routing entry). Out of scope for v1.

### Per-user install cache

`DockerBackend` bind-mounts `<user_home>/.cache` to `/root/.cache` inside the per-session container. uv/pip caches survive across the user's sessions, so cold-install latency only hits the first session.

Disable via `ADK_CC_DISABLE_INSTALL_CACHE_MOUNT=1` for truly stateless containers (every session reinstalls everything).

### Session scratch retention

Per-session scratch dirs at `<user_home>/.sessions/<session>/` accumulate. `scripts/scratch_reaper.py` reaps dirs older than `ADK_CC_SESSION_SCRATCH_RETENTION_DAYS` (default 7). Wire as cron / systemd timer:

```bash
# /etc/cron.daily/adk-cc-scratch-reaper
0 3 * * * /usr/bin/python3 /opt/adk-cc/scripts/scratch_reaper.py \
  --root /var/lib/adk-cc/wks --max-age-days 7
```

User home dirs are never reaped — they're the user's persistent state.

### Migration from pre-per-user layouts

Existing tenant-shared deployments (`<root>/<tenant>/...` with files at the tenant root, no `<user>/` nesting) need a one-time migration per tenant. Two cases:

1. **One-user-per-tenant**: `mv <root>/<tenant>/* <root>/<tenant>/<user>/`. Trivial.
2. **Tenant-shared files**: pick a strategy — copy to a "shared" user home, copy into every user's home, or archive and start fresh. Operator-decided.

Take a snapshot before migrating; rollback = restore from snapshot.

Tasks dir: `mv ~/.adk-cc/tasks/<tenant>/<session>/*.json <root>/<tenant>/<user>/.adk-cc/tasks/<session>/`. Requires session→user mapping (from Postgres `sessions` table joined on user_id).

## Production readiness checklist

Mark each before serving real users. ✓ = covered by adk-cc; ⚠️ = partial / has caveats; ✗ = operator must add.

### Security

- ✓ **Fail-closed auth.** `make_app` refuses to start without an extractor unless `ADK_CC_ALLOW_NO_AUTH=1` is set.
- ✓ **JWT validation.** Signature against JWKS (TTL-cached), exp/nbf, iss, aud, configurable user/tenant claims.
- ✓ **Sandbox isolation.** DockerBackend per-session containers: read-only rootfs, `cap_drop=ALL`, `no-new-privileges`, `network_mode=none` by default, mem/cpu/pids limits, unprivileged user.
- ✓ **Credentials encrypted at rest.** Fernet, key from env / secret manager.
- ⚠️ **Agent → Docker daemon trust.** Agent pod has full Docker daemon API on the sandbox VM. Bounded by mTLS + network ACL but still wide. Tightening to a thin RPC service exposing only the `SandboxBackend` contract is a Stage-2 follow-up.
- ⚠️ **Permissions YAML.** `ADK_CC_PERMISSIONS_YAML` schema is documented in `adk_cc/config/settings_loader.py` but the loader doesn't lint at startup; bad rules surface on first denied call.
- ✗ **HTTP rate limiting.** Per-tenant tool-call rate cap exists (`ADK_CC_QUOTA_PER_MINUTE`). No HTTP-level rate limit (login throttling, per-IP). Add at the ingress (nginx, Envoy, ALB) or via a middleware.
- ✗ **Audit log integrity.** `AuditPlugin` writes append-only JSONL. No tamper-evidence (signed receipts, hash chain, external sink). For regulated workloads, ship the JSONL to an immutable store (S3 Object Lock, append-only Splunk/Elastic).
- ✗ **Dependency CVE scanning.** No CI yet; wire `uv pip install` + `pip-audit` or `trivy` into a pre-deploy check.
- ✗ **Secret rotation.** No documented procedure for rotating `ADK_CC_CREDENTIAL_KEY` (would need re-encrypting all stored credentials), JWKS keys (rolling), Postgres passwords. Plan one before launch.

### Reliability

- ✓ **Multi-worker safety.** `JsonFileTaskStorage`, `EncryptedFileCredentialProvider`, `JsonFileTenantResourceRegistry` all use `filelock` so multiple uvicorn workers don't race.
- ⚠️ **Single sandbox VM.** Capacity planning assumes ~10 concurrent sessions × 4 GB on a 16-core / 96 GB host (good for ~500 users; see `02-architecture.md` §5.5). Past that, multi-VM scaling via consistent hashing on `session_id` is documented but not implemented.
- ✗ **Container leak reaper.** If the agent pod crashes mid-session, the per-session container can orphan. The runbook in `04-deployment-sandbox.md` documents the manual reap; production should add a cron / systemd timer that runs `docker ps --filter label=adk-cc-session --format ...` and reaps containers Up >1h with no agent reachability.
- ✗ **Idle-timeout watchdog.** `DockerBackend` only cleans up on `after_run_callback`. A model that produces long pauses keeps a container hot. Add a watchdog if cost matters.
- ✗ **Session timeout.** ADK sessions don't expire by default. Operators wanting hard caps wire it via the session service or a janitor.
- ✗ **LLM retry / circuit-breaker.** LiteLLM has internal retries; no surfaced policy for transient errors above that. A flaky model server can spike user-facing 500s.
- ✓ **Context-length guardrail.** ADK's `EventsCompactionConfig` runs post-invocation token-threshold compaction via `LlmEventSummarizer` (set `ADK_CC_COMPACTION_TOKEN_THRESHOLD` + `ADK_CC_COMPACTION_EVENT_RETENTION`; optional dedicated compaction model via `ADK_CC_COMPACTION_MODEL`). adk-cc adds `ContextGuardPlugin` for pre-flight WARN logging and fail-soft REJECT (`ADK_CC_MAX_CONTEXT_TOKENS`, `ADK_CC_CONTEXT_WARN_TOKENS`, `ADK_CC_CONTEXT_REJECT_TOKENS`) to catch the rare turn that jumps past the window in one step before ADK can compact. See `02-architecture.md` §7.5.

### Observability

- ✓ **Tool-call audit.** `AuditPlugin` writes one JSONL line per tool attempt — including denials.
- ⚠️ **Tracing.** ADK emits OpenTelemetry spans if a tracer is configured at process start. Wire `OTEL_EXPORTER_OTLP_ENDPOINT` and add an `OpenTelemetryInstrumentor` in your factory; otherwise traces are dropped.
- ✗ **`/healthz`.** No liveness / readiness endpoint. Add one in your factory: `@app.get("/healthz")` returning 200 if Postgres is reachable.
- ✗ **Prometheus metrics.** Nothing exposed today. Useful series to add: request latency p50/p95/p99 per route; tool-call count + error rate per tool; sandbox container count; quota denial count; auth failure count.
- ✗ **Structured logs.** Default logging is unstructured Python `logging`. Wire a JSON formatter (e.g. `python-json-logger`) so log aggregators index fields.
- ✗ **SLI / SLO.** Define before launch: "p95 tool-call latency under X", "session creation success rate above Y", "auth failure rate below Z".

### Operations

- ✗ **Agent process Dockerfile.** Only `Dockerfile.sandbox` ships (for the per-session container, on the sandbox VM). The agent pod itself needs its own Dockerfile — straightforward (`FROM python:3.12-slim`, `uv pip install -e .`, entrypoint to uvicorn) but you have to write it. Check it in alongside the K8s manifests in your deployment repo.
- ✗ **K8s manifests / Helm chart.** Not provided. Minimum: Deployment, Service, ConfigMap (env), Secret (creds + JWT keys + Docker mTLS certs), PersistentVolumeClaim, NetworkPolicy (allow only IdP egress + Postgres + sandbox VM Docker port).
- ✗ **Graceful shutdown.** `uvicorn` handles SIGTERM but ADK doesn't have a documented session-flush hook. In-flight `run_bash` calls die when the pod terminates. Acceptable for stateless tools; for long-running ones, drain via load balancer first.
- ✗ **Backup / restore.** Five state stores; document procedures for each:
  - Postgres (sessions): standard `pg_dump` / `pgBackRest`.
  - `ADK_CC_TASKS_DIR` (tasks): rsync / volume snapshot.
  - `ADK_CC_CREDENTIAL_STORE_DIR` (credentials): rsync; **also back up the Fernet key separately** — the encrypted blobs are useless without it.
  - `ADK_CC_TENANT_REGISTRY_DIR` (mcp / skill registry): rsync.
  - `ADK_CC_TENANT_SKILLS_DIR` (skill folders): rsync.
  - Workspaces (`/var/lib/adk-cc/wks` on sandbox VM): volume snapshot per tenant SLA.
- ✗ **Log rotation.** `ADK_CC_AUDIT_LOG` is appended forever. Use `logrotate` (size-based or daily, with copytruncate so the open fd keeps writing).

### Multi-tenancy

- ✓ **Tenant scoping.** Workspaces, sessions, tasks, plans, MCP, skills, credentials all scoped per `tenant_id`.
- ✓ **Tool-call rate cap.** Per-tenant via `ADK_CC_QUOTA_PER_MINUTE`.
- ⚠️ **Per-session resource limits.** Sandbox container has fixed mem/cpu/pids per `ADK_CC_SANDBOX_*` env vars — same for all tenants. Differential limits per tenant tier require subclassing `DockerBackend`.
- ✗ **Storage quotas.** Workspace size, plan history depth, task count — unbounded today. A misbehaving session can fill `/var/lib/adk-cc/wks/<tenant>/<session>/` arbitrarily.
- ✗ **Tenant lifecycle.** No onboarding (provision workspace + tenant dirs), offboarding (delete all artifacts), GDPR delete. Operators script this against the documented filesystem layout.
- ✗ **LLM token budget.** No per-tenant cap on LLM tokens consumed. Cost runaway is possible. Add an `LlmCostPlugin` that tracks tokens via LiteLLM hooks and trips the quota.

### Configuration

- ✓ **Env-driven config.** `.env.example` documents every knob.
- ✗ **Startup validation.** Bad values (typo'd env, malformed permissions YAML, unreachable Postgres) often surface only on the first request. Add eager probes in `make_app` — connect to Postgres, fetch JWKS, ping Docker daemon — fail fast at boot.

### Tests / CI

- ⚠️ **e2e.** `tests/e2e_features.py` covers JWT auth, MCP admin + resolver, skill upload + resolver. Runs in-process via FastAPI TestClient. Not yet pytest-shaped, no CI runner.
- ✗ **Unit tests.** Ad-hoc smoke tests inside commits; no permanent suite. Promote to `tests/unit/test_*.py` with pytest.
- ✗ **CI.** No GitHub Actions / GitLab CI / etc. Wire a basic pipeline: `uv sync`, `uv run pytest`, `pip-audit`, optionally `ruff` / `mypy`.
- ✗ **Regression fixtures.** No stub MCP server for tool-call roundtrip; no model-deterministic harness for skill execution. See `tests/e2e_features.py` "What's NOT covered in-process" note.

## Day-2 ops

### Common log lines

| Where | What it means |
|---|---|
| `RuntimeError: ADK_CC_AGENTS_DIR must be set for make_app()` | Env var missing; refuse to start. |
| `RuntimeError: make_app(): no auth extractor configured` | Set `ADK_CC_JWT_JWKS_URL` (prod) or `ADK_CC_AUTH_TOKENS` (dev only). |
| `EncryptedFileCredentialProvider needs a Fernet key` | Set `ADK_CC_CREDENTIAL_KEY`. |
| `SandboxViolation: refusing to exec in prod-shaped path` | NoopBackend in production. Switch to `ADK_CC_SANDBOX_BACKEND=docker`. |
| `TenantMcpToolset: skipping server '<name>' for tenant '<id>'` | MCP server unreachable / misconfigured. Check the tenant's registered URL + credential. |
| `503 jwks fetch failed` | IdP JWKS endpoint unreachable. Check egress. |

### Incident: stale-session error during HITL confirmation

Symptom: `ValueError: The last_update_time provided in the session object is earlier than the update_time in storage` from `sqlite_session_service.py:386`. Typically fires after several turns, especially across a tool-confirmation pause/resume (e.g. `enter_plan_mode` / `exit_plan_mode` / a `run_bash` confirmed via the permission engine).

Root cause is upstream: ADK's pause/resume cycle bumps SQLite's `update_time` through code paths that don't refresh the SSE generator's in-memory session reference. We mitigated the contribution from our state writes by prefixing runtime handles (`sandbox_backend`, `sandbox_workspace`, `tenant_context`) with `temp:` so ADK skips them in `extract_state_delta`. The remaining churn comes from ADK's own state mutations during the resume.

**Dev workaround #1 (no persistence)**: bypass SQLite session storage:

```bash
adk web . --session_service_uri=memory://
```

In-memory sessions have no optimistic-locking → no stale-session race. Sessions vanish on process restart.

**Dev / production workaround #2 (retry-on-stale)**: opt into the session-retry wrapper that adk-cc ships in `plugins/session_retry.py`:

```bash
export ADK_CC_SESSION_RETRY_ON_STALE=1
```

Patches `SqliteSessionService.append_event` and `DatabaseSessionService.append_event` at module import. On stale-session ValueError it fetches a fresh session, syncs `last_update_time` (and `event_sequence`, post-PR-#4752), retries the append once. Logs every retry at WARNING. Single-retry semantics — if the second attempt also fails, it raises (genuine concurrent-writer conflict the application should surface).

This works for SQLite *and* Postgres. Caveats:
- A real concurrent writer can still lose its event after the retry — that's by design (only one writer wins).
- If retries fire frequently, it's a signal of underlying contention that the wrapper is just papering over.

### Incident: orphan sandbox containers

Triggered by an agent pod crash. To clean up on the sandbox VM:

```bash
docker ps --filter label=adk-cc-session --format '{{.Names}} {{.Status}}'
# Reap anything Up >1h: stop + remove
docker ps -q --filter label=adk-cc-session --filter status=running \
  | xargs -I {} sh -c 'docker stop {} && docker rm -v {}'
```

### Incident: credential decryption fails after key rotation

Encrypted blobs from the OLD key cannot be decrypted with the NEW key. To rotate cleanly: decrypt all blobs with the old key (programmatic loop), re-encrypt with the new key, then swap `ADK_CC_CREDENTIAL_KEY` and restart. There is no automated rotation tool yet.

### Upgrades

`uv sync` + restart works for adk-cc-internal changes. Verify after upgrade:

1. `tests/e2e_features.py` passes.
2. Live smoke test (step 8 above).
3. Check `docker ps` on the sandbox VM during the smoke test — per-session containers should still spawn and disappear normally.

If a release changes a state-store on-disk format (tasks JSON, credential blobs, plan files): the release notes will call it out and provide a migration script. Today's formats:

- Tasks: `ADK_CC_TASKS_DIR/<tenant>/<session>/<task_id>.json` — see `adk_cc/tasks/model.py` for the Pydantic schema.
- Credentials: `<store_dir>/<tenant>/<key>.enc` — Fernet ciphertext.
- Tenant registry: `<registry_dir>/<tenant>/mcp.json` — JSON list of `McpServerConfig`.
- Skills: `<skill_root>/<tenant>/<name>/SKILL.md` (+ scripts) — ADK skill format.
- Plans: `<workspace>/.adk-cc/plans/<timestamp>-<slug>.md` — Markdown.
