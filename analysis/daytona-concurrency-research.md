# Daytona under concurrent load — research notes (self-hosted focus)

Question investigated: **how does Daytona handle massive concurrent sandbox
creation/run requests, and what happens when capacity runs out?** Asked to
decide whether adk-cc's DaytonaBackend needed client-side pooling/queueing.
Answer: Daytona pools (warm pools) and partially queues (builds only) —
and on true capacity exhaustion the API **rejects snapshot-creates with a
400**, so the client's backoff IS the queue. That finding produced the
capacity-backoff fix (`be14513` on main).

## Sourcing (honesty box)

- Code read at **v0.190.0 — the last public tag**; the GitHub repo went
  private in June 2026. Official docs describe the cloud product; the
  self-hosted behavior below comes from the code itself.
- Key files (paths as of v0.190.0; working copies were saved to this
  session's ephemeral job tmp — treat upstream as the reference):
  - `apps/api/src/sandbox/services/sandbox-warm-pool.service.ts`
  - `apps/api/src/sandbox/services/sandbox.service.ts`
  - `apps/api/src/sandbox/services/runner.service.ts`

## Architecture (concurrency-relevant slice)

- **Control plane** (NestJS, `:3000`): REST API + TypeORM (Postgres) +
  Redis (locks, caches) + an event-driven job pipeline. Desired-state
  model: `Sandbox.desiredState` vs `state`, reconciled asynchronously.
- **Runners**: separate hosts that PULL work; each registered runner row
  carries `region`, `class`, `unschedulable`, capacity fields, and a
  computed **`availabilityScore`** (health/usage-derived).
- **Toolbox proxy** (`:4000`, Go): per-operation exec/file IO — stateless
  from the control plane's POV, so exec load doesn't queue centrally at
  all; it lands directly on the runner hosting the sandbox.

## Warm pool mechanics

- A **`WarmPool` entity** = one pool config per exact tuple *(snapshot,
  target region, cpu, mem, disk, gpu, osUser, env)* with a fixed `pool`
  target size. No dynamic sizing, no burst autoscaling.
- **Claim path** (`fetchWarmPoolSandbox`): a create request first tries to
  adopt a `STARTED`, unassigned-org sandbox matching the tuple —
  candidates fetched `ORDER BY RANDOM()` capped by `warmPool.candidateLimit`,
  excluding runners that are `unschedulable` or below
  `runnerScore.thresholds.availability`; a per-sandbox Redis lock (10s)
  guarantees single release. Claim = instant start (the pool's entire
  point).
- **Top-up**: a `@Cron(EVERY_10_SECONDS)` `warmPoolCheck` counts live pool
  sandboxes per config (excluding ERROR/BUILD_FAILED) and emits
  `TOPUP_REQUESTED` events for the deficit, under a 720s Redis lock so
  only one worker tops up. A claim also triggers an immediate top-up via
  the `ORGANIZATION_UPDATED` event.
- **No warm-pool config for a snapshot** → a 60s Redis skip-marker; every
  create for that snapshot is a cold start.
- Known gap (upstream issue #3289): the top-up loop has **no failure
  circuit-breaker** — if creates persistently fail, it retries every 10s
  forever.

## What happens at exhaustion (the load-bearing findings)

1. **Pool exhausted, runners available** → cold start: normal create
   scheduled on a selected runner. Slower (pull image / start container),
   but succeeds.
2. **Runners exhausted** (none in region pass the filters):
   `getRandomAvailableRunner` throws `BadRequestError('No available
   runners')` →
   - **snapshot-based create: HTTP 400, immediately. There is NO
     server-side queue for snapshot creates.**
   - **build-based create** (`buildInfo`): the sandbox row parks in
     `PENDING_BUILD` and IS effectively queued — it proceeds when a
     runner frees up. (Queueing exists only on the slow path.)
3. **`availabilityScore` degradation** quietly shrinks effective capacity
   before hard exhaustion: scored-out runners are excluded from both
   warm-pool claims and cold-start selection.
4. **Self-hosted custom regions have no org quota enforcement** (quotas
   are a cloud construct). So the only backpressure a self-hosted client
   ever sees is: `400 "No available runners"`, `429` (rate limiter),
   `5xx`. **Client-side backoff is the only queue.**

## Consequence for adk-cc (implemented)

The DaytonaBackend previously mapped every non-2xx create failure to a
permanent `SandboxViolation` — so transient capacity exhaustion hard-failed
the session, and the tenancy plugin's retry-on-next-tool-call produced an
unbounded, jitterless herd. Fixed on main (`be14513`):

- `SandboxCapacityError(SandboxViolation)` (retryable, carries
  `retry_after`) raised for `429` (honoring `Retry-After` /
  `X-RateLimit-Reset`), `5xx`, and specifically `400` matching
  `"No available runners"`. Other 400s / 401 / 403 / 404 stay permanent
  and fast-fail.
- Create+poll wrapped in bounded exponential backoff with equal jitter:
  attempts=6, base 0.5s, cap 8s, total wall-clock 45s
  (`ADK_CC_DAYTONA_CREATE_{MAX_ATTEMPTS,BACKOFF_BASE_S,BACKOFF_CAP_S,TOTAL_WAIT_S}`).
  One idempotency key reused across retries so an ambiguous 5xx can't
  spawn duplicate sandboxes. Tests: `tests/test_daytona_backend.py`
  (429-then-200, "No available runners"-then-200, permanent-400 fast-fail,
  bounded exhaustion, Retry-After honored).

## Operator guidance (self-hosted deployment)

- **Size WarmPool configs for the expected burst**, per snapshot×region
  tuple — the pool refills at best every 10s, so a burst larger than
  `pool` eats the pool then cold-starts, then 400s when runners saturate.
- Watch `runnerScore.thresholds.availability` — an aggressive threshold
  turns degraded-but-alive runners into invisible capacity loss.
- Snapshot creates are the fast path but **don't queue**; build creates
  queue (`PENDING_BUILD`) but pay build latency. adk-cc v1 uses snapshots
  (`ADK_CC_DAYTONA_SNAPSHOT`) + client backoff, which matches.
- The `DEFAULT_SNAPSHOT` config decides what an empty `snapshot` field
  resolves to — keep it pointing at a pooled snapshot or every default
  create is a cold start.
