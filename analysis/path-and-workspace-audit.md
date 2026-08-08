# Paths, roots and workspaces: an audit

Research only — nothing changed. 2026-08-08.

Scope: every directory notion adk-cc uses to locate a run's code, data and
resources, and how each one can quietly go wrong. Several findings below are
not theoretical — they bit during this session's work and are marked
**OBSERVED**.

## 1. The four coordinate systems

adk-cc simultaneously holds four different ideas of "where":

| space | root | who speaks it |
|---|---|---|
| **agent host** | `WorkspaceRoot.abs_path` | file tools, `resolve()`, fs allow-lists, checkpoints |
| **runtime** | `backend.container_cwd()` — `/workspace` (docker, sandbox_service), Daytona's path, identity (noop/ssh) | `run_bash`, skill scripts, anything the model reads in tool output |
| **server data** | `deployment.data_dir()` | identity, credentials, trust, tasks, audit |
| **session store** | `FileSessionService._base/projects/<user>/sessions/` | turn history |

Bugs cluster where a value crosses a boundary without translation. Every
confirmed defect in this audit is a crossing, not a bad path.

## 2. OBSERVED defects

### 2.1 The model is told container paths; the file tools reject them
`container_cwd()`'s own docstring: *"The workspace hint surfaces THIS to the
model — not the host path — so an absolute path it constructs actually
exists where its tools run."* The model duly forms
`/workspace/.adk-cc/skill-runtime/…`. But `read_file` → `resolve()` passes
absolute paths through untouched, and `fs_read.allows()` is built from HOST
paths (`{ws.abs_path}/**`). Result: **`read denied by fs_read` for a file
that is inside the workspace**, reachable under its other spelling.

`_to_container_path()` exists on DockerBackend and SandboxServiceBackend.
**There is no inverse anywhere.** Proposed minimal fix (not applied) is in the
conversation: normalize in `resolve()` using `container_cwd()`, which is
generic across backends and identity for host-exec ones.

### 2.2 `realpath` vs raw path — the same directory, two keys
`WorkspaceRoot.__post_init__` canonicalises with `os.path.realpath`. On macOS
`/var → /private/var`, `/tmp → /private/tmp`. Anything keyed by the raw
string misses:
- `analysis_env._verified` is keyed `(ws.abs_path, token)` — correct only
  because `abs_path` is already canonical.
- **OBSERVED twice in test harnesses**: comparing against a raw `mkdtemp()`
  path silently matched nothing, once reporting "the cache is empty" and once
  making every `docker exec` fail with `chdir … no such file or directory`.
- `skill_trust._key()` independently does `expanduser().resolve()`. Two
  modules, two canonicalisation implementations, no shared helper.

Latent: any future map keyed by a path supplied from outside
`WorkspaceRoot` will disagree with one keyed inside it.

### 2.3 `data_dir()` resolves differently per deployment
`ADK_CC_DATA_DIR` → **desktop mode only**: `ADK_CC_DESKTOP_DATA` → else
`~/.adk-cc`. So setting only the desktop alias in a **web** deployment
silently falls through to the operator's real home directory.
**OBSERVED**: a web e2e wrote a session into the real `~/.adk-cc` alongside 25
unrelated ones, and its own lookups found nothing. A test that believes it is
isolated and is not is the dangerous shape here — production config has the
same failure mode with `ADK_CC_DESKTOP_DATA` set in a web unit file.

### 2.4 Session store does NOT live under `data_dir()`
`FileSessionService._base/projects/<user>/sessions/`, with `_base` supplied
separately. So "point the data dir somewhere else" does not relocate history.
**OBSERVED**: setting `ADK_CC_DATA_DIR` still left the run reading a store
with 25 pre-existing sessions. Two roots that look like one.

### 2.5 Runtime HOME on a read-only rootfs
DockerBackend runs `read_only=True` with the image's `HOME=/home/sandbox` on
that rootfs, so every cache write failed (`uv`, pip, npm).
**OBSERVED**: `failed to initialize cache at /home/sandbox/.cache/uv:
read-only file system`. LocalContainerBackend had solved this long ago
(`_CONTAINER_HOME=/tmp/adk-home`); the sibling backend never got it. Fixed
this session (`CONTAINER_HOME` inside the workspace mount) — recorded because
the *pattern* recurs: **two backends implementing one concept, one of them
missing a lesson the other learned.** Same shape as the missing `uv`, and as
the missing config-signature on container adoption.

### 2.6 Docker's archive API rejects non-volume destinations
With `read_only=True`, `put_archive` to `/tmp` returns
`400 container rootfs is marked read-only` **even though a shell in the same
container writes there happily**. Measured both ways. So "can the runtime
write here?" has two different answers depending on which API asks.

## 3. Latent, not yet observed

### 3.1 `resolve()`'s remote branch is deliberately lexical, the local one is not
Remote workspaces skip `expanduser`/`realpath` on purpose — they would consult
the wrong machine's filesystem. The local branch calls `.resolve()`. So the
same user input normalises differently depending on a flag on the workspace.
Any code comparing a resolved path to a lexical one is a latent mismatch;
symlinked project roots are the likely trigger.

### 3.2 `/tmp` is permanently in the allow-list
`_system_temp_roots()` adds `/tmp`, `/private/tmp` and `$TMPDIR` to BOTH read
and write allow-paths, justified as the universal scratch convention. Two
consequences worth naming: workspace isolation does not extend to `/tmp`, so
two sessions share a namespace there; and `/tmp` content is outside
checkpoint/undo by design, so an agent that writes results there has produced
something the safety net does not cover.

### 3.3 Digest-keyed skill runtime paths churn
`.adk-cc/skill-runtime/<skill>/<digest>/`. Any edit to a skill changes the
digest, so every path the model learned is stale. #113's stable `current/`
slot addresses this; worth pairing with §2.1, since the paths that churn are
exactly the ones the model cannot read back.

### 3.4 `default_workspace()` falls back to CWD
`ADK_CC_WORKSPACE_ROOT` → else **the process CWD**. For `adk web .` that is
intended. For a service started from an unexpected directory it silently
roots the workspace wherever systemd/launchd happened to put it. Related to
#88 (project skills once loaded from the server's CWD).

### 3.5 24 path-shaped env vars, several overlapping
`ADK_CC_DATA_DIR`, `ADK_CC_ADMIN_DATA_DIR`, `ADK_CC_IDENTITY_DIR`,
`ADK_CC_CREDENTIAL_STORE_DIR`, `ADK_CC_TASKS_DIR`, `ADK_CC_TENANT_REGISTRY_DIR`,
`ADK_CC_MEMORY_ROOT`, `ADK_CC_WIKI_ROOT`, `ADK_CC_CODEX_STORE_DIR`,
`ADK_CC_WEB_RUNTIME_DIR`, `ADK_CC_SSH_CONTROL_DIR`, … Most default *under*
`data_dir()`, so they are overrides on a tree whose own root resolves by the
three-way rule in §2.3. The env-var refactor plan (#project_env_var_refactor)
already proposes one `DATA_DIR` root; §2.3 and §2.4 are evidence for it.

## 4. The pattern behind most of this

Two recurring shapes explain nearly every finding:

1. **A value crosses a boundary without translation** (§2.1, §2.2, §2.6).
   The fix is never "handle this path specially" — it is to make the crossing
   explicit and put it in one place.
2. **Sibling implementations drift** (§2.5, and historically: `uv` present in
   the Daytona image but not the Docker one; the container-config signature
   present in LocalContainerBackend but not DockerBackend). Whenever two
   components implement one concept, expect the lesson learned in one to be
   missing from the other.

## 5. Suggested order, if this becomes work

1. §2.1 — user-visible today, ~5 lines, no permission change.
2. §2.3 + §2.4 — silent cross-deployment data leakage; also makes tests
   honestly isolatable, which §2.3 proved they are not.
3. §2.2 — one shared canonicalisation helper; removes a whole class.
4. §3.1, §3.4 — audit-only until something reproduces.

Nothing here is fixed. §2.5 and §2.6 were fixed this session and are recorded
for the pattern, not as open items.
