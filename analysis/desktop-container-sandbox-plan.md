# Desktop container sandbox (Docker / Podman) — plan

Give the **local desktop app** an opt-in execution sandbox: when Docker or Podman
is available, run the agent's **shell commands** inside a container that
bind-mounts the project **in-place** (edits still land in the user's real files),
but isolates the **host** — process table, network policy, resource limits, and
the rest of the filesystem. Falls back to host-exec (today's `noop`) when no
runtime is present or the user leaves it off.

## Decisions (locked in chat)
1. **Isolation scope = host only.** The project is bind-mounted read-write
   in-place; a bad `rm -rf ~`, rogue `curl | sh`, fork bomb, or resource hog is
   contained to an ephemeral container, not the machine. The agent still edits
   real project files by design.
2. **Selection = opt-in.** Host-exec stays the default; the user enables the
   container sandbox in Settings. Auto-detect surfaces availability but never
   changes execution semantics silently.
3. **Network = allowed by default.** pip/npm/git/curl work out of the box; a
   per-project toggle can lock it to `--network none`.

## What the sandbox does and does NOT contain (be honest about the boundary)
- **Contains:** `run_bash` (the only arbitrary-code path) — its process tree,
  network, resource use, and host-filesystem reach (only mounted paths are
  visible).
- **Does NOT re-contain:** the file tools (`read_file`/`write_file`/`edit_file`)
  and the desktop file **panel** stay **host-direct** — for an in-place bind
  mount they operate on the exact same bytes, and they're already scoped by the
  permission engine (protected-path floor + out-of-project write gate) at the
  tool layer, independent of backend. Routing them through the container would
  add `docker cp` latency + break binary-safety for zero isolation gain (both
  paths are already bounded to project + granted dirs). So the guarantee is
  precisely: *shell execution is isolated; file writes remain permission-gated,
  same as today.*

## Current system (investigated)
- `SandboxBackend` ABC (`sandbox/backends/base.py`): `exec` / `exec_stream` /
  `read_text` / `write_text` / `read_bytes` / `write_bytes` / `ensure_workspace` /
  `container_cwd` / `close`, plus `configure_runtime_env` + `_runtime_env()` for
  on-demand secret/env injection.
- `NoopBackend` (host exec): `read_text`/`write_text` are host-direct with
  `fs_read/fs_write.allows()` guards; `exec` spawns a host subprocess, merges
  `_runtime_env()`, and enforces a prod-shaped-cwd ack + cwd-is-dir guard.
- The **bash tool** calls `backend.exec(cwd=ws.abs_path)` — the **host** path;
  the **file tools** call `backend.read_text/write_text(str(p))` (host paths);
  the **workspace-hint** plugin shows the model `backend.container_cwd(host)` (so
  a backend that remaps advertises the remapped root). The desktop file **panel**
  (`desktop_files.py`) reads the host fs directly, never via the backend.
- Selection: `deployment.sandbox_backend_name()` reads `ADK_CC_SANDBOX_BACKEND`
  (default `noop`); `make_default_backend(...)` constructs it AND wires
  `configure_runtime_env(...)` (user secrets + operator `SandboxEnvSpec` + the
  least-privilege declared-secrets allowlist). `TenancyPlugin` builds one backend
  per session, calls `ensure_workspace(ws)`, `close()` at session end.
- **Desktop today = `noop`.** The Tauri sidecar (`src-tauri/src/main.rs`) sets
  `ADK_CC_DESKTOP/DATA/TENANCY_MODE/…` but NOT `ADK_CC_SANDBOX_BACKEND`, so it
  runs on the host; `noop_ack_host_exec()==is_desktop()`. Workspace is **in-place**
  (`desktop_workspace.session_workspace_path()` == the project repo root). Undo is
  host-side shadow-git (`desktop_checkpoint.py`), operating on the same files.
- An existing remote `DockerBackend` targets a REMOTE daemon (dedicated per-session
  workspace on a VM, `chown -R 1000:1000`, `/workspace` remap via
  `_to_container_path`, read-only rootfs, `network=none`, helper container for
  `mkdir`). **Wrong for local in-place** — don't reuse as-is; it also never injects
  `_runtime_env()` (a latent secrets gap).
- `Dockerfile.sandbox` exists (python:3.12-slim + git/ripgrep/fd/build-essential +
  data stack + uid-1000 user); swappable via `ADK_CC_SANDBOX_IMAGE`.
- `is_noop_backend()` gates the artifact tools off under `noop`; a real container
  backend flips them on (free side benefit).

## Recommended shape (implementer's call — flag if you disagree)
- **`LocalContainerBackend(NoopBackend)`** (`name = "container"`): inherit the
  host-direct `read_text`/`write_text` (+ fs guards) and `_runtime_env`
  resolution; **override only `exec` / `exec_stream`** (containerize the shell)
  plus `ensure_workspace` (container lifecycle + mounts) and `close` (teardown).
  This is small and correct precisely because only the shell needs isolation.
- **Mount the project at its IDENTICAL host path**, not `/workspace`:
  `-v <ws_abs>:<ws_abs>` (+ each granted root), `--workdir <the host cwd the tool
  passed>`. Leave `container_cwd` as identity (inherited) → the model is told the
  real host path and paths are transparent inside and out. No `_to_container_path`
  remap. (Rare fallback: if the project path collides with an image system dir —
  e.g. `/root` — fall back to a `/workspace` remap for that session.)
- **Drive it via the `docker` / `podman` CLI**, not docker-py. Podman is a drop-in
  `docker` CLI and both transparently front their macOS/Windows VM (Docker Desktop,
  `podman machine`); the SDK does not handle Podman cleanly. Detection is
  `docker`/`podman` on PATH + a cached `info` probe.

## Plan (phased)

### Phase 1 — Runtime detection + connection
`sandbox/backends/container_runtime.py`:
- `detect_runtime() -> Runtime | None` — probe `docker` then `podman` on PATH
  (incl. `.exe` on Windows), run `<rt> info` (off the event loop, short timeout)
  to confirm the daemon/VM is up; return `{name, version, cli_path}`. Cached
  (module-level; invalidated on Settings change). Order/override via
  `ADK_CC_SANDBOX_RUNTIME=auto|docker|podman`. Never raises → `None` = host-exec.
- **Windows / WSL:** Docker/Podman Desktop run the engine on the WSL2 backend but
  expose a Windows-side `docker.exe`/`podman.exe`; the native-Windows sidecar
  talks to that CLI transparently (a reason to prefer CLI over SDK), and Desktop
  handles the Windows-path bind mount. NOT covered: docker/podman inside a **bare
  WSL distro** with no Desktop — needs a deferred `wsl.exe <rt> …` bridge with
  `C:\ ↔ /mnt/c/` translation (slow drvfs, uid quirks).

### Phase 2 — LocalContainerBackend (host-only, in-place)
`sandbox/backends/local_container_backend.py`, `name = "container"`:
- **Mounts:** the workspace root + every current `list_granted_roots(ctx)` at
  their identical host paths (rw). The project path must be within the runtime's
  shared paths (Docker Desktop file-sharing; `podman machine` shares `$HOME`); a
  path outside → fall back to host-exec with a clear reason.
- **Ownership:** run as the host user so writes come back correctly owned —
  Docker `--user $(id -u):$(id -g)`, Podman rootless `--userns=keep-id`. macOS
  Docker Desktop maps ownership via the VM regardless; verify on real hardware.
- **Writable env for real dev flows:** do **not** use a read-only rootfs by
  default (that breaks `pip install` / `npm install`); let installs land in the
  container's ephemeral writable layer (gone on `close`). Keep tmpfs `/tmp`, a
  **writable HOME** (tmpfs, or an opt-in per-project cache dir under desktop data
  so pip/npm caches persist across sessions), `--security-opt no-new-privileges`,
  `--cap-drop ALL`, and `--pids-limit` / `--memory` / `--cpus` (generous desktop
  defaults, all tunable). read-only rootfs stays an opt-in hardening.
- **Network:** default on (bridge); a per-project "lock network" setting →
  `--network none`; also honor the per-exec `NetworkConfig`.
- **Lifecycle:** one container per session (`adk-cc-<session>`, `sleep infinity`),
  reused across turns; `_ensure_container` (lazy, off-loop) creates or re-attaches;
  `close()` stops+removes; a startup sweep reaps orphaned `adk-cc-*` from crashes.
- **`exec` / `exec_stream`:** `<rt> exec -w <cwd> <name> bash -lc 'timeout <N>
  <cmd>'` — the container-side `timeout` guarantees the in-container process is
  actually killed on deadline (killing only the `<rt> exec` subprocess can orphan
  it). `exec_stream` = a piped `Popen` yielding `ExecChunk`s, terminating with the
  `result` chunk (exit code from wait). Demux stdout/stderr via separate pipes.
- **Secret / env injection (API keys etc.) — first-class.** The plumbing is
  inherited (`make_default_backend` → `configure_runtime_env`). Per exec resolve
  `env = await self._runtime_env()` (resolve-at-exec, 15s TTL, so a key added
  after the container starts reaches the next command — no recreate; declared-
  secrets allowlist already applied). Inject WITHOUT leaking:
  - set each `KEY=VALUE` in the `<rt>` **subprocess's own env** and reference by
    NAME only on argv (`<rt> exec -e KEY1 -e KEY2 …`). `-e KEY` (no `=value`)
    forwards the value from the CLI process — so secret VALUES never touch argv
    (`ps` / shell history) or container config;
  - inject **at exec, never at `<rt> run`** (create-time env persists in `inspect`
    and can't update); no `--env-file` (plaintext on disk);
  - `SecretRedactionPlugin` already scrubs known secret values from model-visible
    output backend-agnostically — verify it wraps container output;
  - network-off + injected key → key present but outbound calls fail (expected);
    say so in the Settings copy.
  - Fix the remote `DockerBackend`'s missing injection in the same pass (one-line
    `environment=` on its `exec_run`).
- **`ensure_workspace`:** the in-place project dir already exists — just verify it
  (no helper container, no chown). Pre-create the container here or lazily on
  first exec.

### Phase 3 — Selection wiring (opt-in) + dynamic-grant handling
- `deployment.py`: `sandbox_mode() -> "host"|"container"` (desktop setting,
  default `host`); `container_runtime_available()` (cached Phase-1 probe).
  `sandbox_backend_name()` stays env-first, but in desktop resolves to `container`
  when the setting is `container` AND a runtime is available — else `noop`,
  logging the reason.
- `make_default_backend`: add a `container` branch →
  `LocalContainerBackend(session_id, tenant_id, workspace_abs_path=…, runtime=…)`;
  any construction/detection failure falls back to `noop` (never breaks a session).
- **Dynamic folder grants:** bind mounts are fixed at container create, but
  `add_granted_root` can fire mid-session. v1: mount the workspace + currently-
  granted roots at create; when a new root is granted, mark the container **stale**
  and recreate it on the next exec (cheap — the layer is ephemeral, the project is
  on the bind mount). Trade-off (documented): recreation drops in-container
  background process state (e.g. a dev server the agent started). A later
  enhancement can mount a broad opt-in parent to avoid recreation.
- Persist the setting in the desktop settings store (global + optional per-project
  override), alongside the existing `/desktop/settings/*`.

### Phase 4 — Image bootstrap
- Default `ADK_CC_SANDBOX_IMAGE` to a small stock image (e.g. `python:3.12-slim`)
  for zero-setup first run; power users point at the richer bundled
  `Dockerfile.sandbox` (build) or a published `adk-cc-sandbox` (pull).
- Settings shows image state (present / missing / pulling) with one-click
  **Pull** / **Build** + progress; a missing image degrades to host-exec with a
  clear reason, never a hard error.

### Phase 5 — Desktop Settings UI + status
- Settings → **Sandbox** section (reuse the `/desktop/settings/*` pattern):
  runtime status ("Docker 27.x detected" / "Podman 5.x" / "none — using host
  execution"), a **Host execution ⇄ Container sandbox** toggle, a network toggle,
  image status + Pull/Build, advanced resource limits, and a note that the project
  path must be within the runtime's shared paths.
- A small **per-session indicator** (composer/header) showing sandboxed-vs-host so
  the user always knows where commands run (and why, if it fell back).

### Phase 6 — Safety interplay + docs
- The permission engine + danger classifier still gate commands (defense in
  depth) — but a contained `rm -rf /` now hits an ephemeral container rootfs +
  the mounted project (project deletion is still possible-by-design, guarded by
  the danger prompt + shadow-git undo), and can't touch the rest of the host.
  Document the boundary from the section above.
- **Undo/checkpoint stays compatible:** shadow-git runs host-side on the same
  bind-mounted files, so it captures container writes unchanged — call this out
  as verified.
- Artifact tools auto-enable under the container backend (`is_noop_backend`
  false).
- `.env.example` + desktop help: `ADK_CC_SANDBOX_BACKEND=container`,
  `ADK_CC_SANDBOX_RUNTIME`, `ADK_CC_SANDBOX_IMAGE`, network default, resource
  limits; the macOS/Windows VM reality (bind-mount perf on large repos, uid
  mapping, shared-path requirement), and that in-place means the project is
  intentionally writable.

## Cross-cutting
- **Off-loop + cached** detection/handshake (the `info` probe blocks); every
  container op runs in `asyncio.to_thread`.
- **Graceful fallback** everywhere — no runtime / no image / daemon down /
  project outside shared paths → host exec + a surfaced "sandbox unavailable"
  reason, never a broken session.
- **Platform prerequisite:** WSL/Windows support presupposes the desktop app runs
  on Windows at all — today the Tauri sidecar launch uses a Unix venv path
  (`.venv/bin/python`), so `.exe` / `Scripts\` handling is a separate packaging
  task. Target macOS/Linux first; Windows-via-Desktop is a small detection add-on
  once packaging lands, the bare-WSL bridge a later opt-in.
- **Testing** (real e2e, dangerous-exec only contained): pure unit tests for
  detection + selection + the stale-on-grant recreate logic (mock the CLI); a
  **live e2e** that SKIPS when no runtime — spins a real container (docker or
  podman), runs only BENIGN commands (`pwd` → asserts it prints the real host
  path; write a file in the mount → assert it appears on the host with the host
  user's ownership; assert an injected `-e KEY` is visible to the command but its
  VALUE never appears in the process argv; network-off toggle → `curl` fails). A
  dangerous command is asserted at the classifier/prompt gate and never executed.
  Playwright UI e2e for the Settings section (runtime detected + fallback copy).

## Suggested build order
Phase 1 (detect) → 2 (backend as a NoopBackend subclass, identical-path mounts,
exec-only isolation) → 3 (opt-in wiring + stale-on-grant) + the live e2e = the
functional core behind the toggle. Then 5 (Settings UI/status) → 4 (image
bootstrap) → 6 (safety/docs). Ship 1–3 first so it's testable before the polish.
