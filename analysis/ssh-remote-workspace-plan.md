# SSH remote workspace — implementation plan (6 separate PRs)

Goal: a desktop project whose workspace lives on a remote device, reached over
SSH. The agent execs/reads/writes there through a new `SshBackend`; the file
panel, git change markers, permission floor, and (eventually) checkpoint/undo
all follow. Alongside, the per-session backend UI gap (badge shows global
settings, not session truth) is fixed as its own PR since the container/host
split benefits immediately.

Granularity decision (settled): **backend is a property of the project**, fixed
per session at creation. No per-session picker in v1 — an SSH project's session
can't meaningfully run the local container backend, so a session-level choice
would mostly offer invalid combinations.

## Standing invariants (apply to every PR)

- **Secrets never on argv** — runtime env reaches the remote via an stdin
  script (`export K=V` lines piped to `ssh <host> /bin/sh -s`), never in the
  ssh command line, never logged (key NAMES only).
- **No password auth, ever.** Keys/agent only, `BatchMode=yes`, strict host
  keys. Setup failure message: "run `ssh <host>` once in your terminal first".
- **Real e2e over mocks**: every PR lands with a live test against a real
  `sshd` (throwaway container on a local port). Benign commands only —
  dangerous-exec strings appear solely in pure classifier tests.
- **v1 scope**: POSIX remotes (`sh`, `cat`, `mkdir -p`, optional `git`).
  Remote exec is NOT containerized (same trust level as host mode, on the
  remote account) — stated in UI copy, never implied otherwise.
- Identical-path model: the workspace IS the remote project root;
  `container_cwd` returns it unchanged (LocalContainerBackend precedent).

## PR 1 — `feat/ssh-transport`: transport foundation

New `agents/adk_cc/sandbox/ssh_transport.py`, shared later by the backend AND
the desktop panel/checkpoint (one multiplexed connection, not N ssh stacks).

- `SshTransport(host)` — ControlMaster lifecycle (`ControlMaster=auto`,
  `ControlPersist=600`, per-host ControlPath under the desktop data dir),
  per-host asyncio lock, `close()`. System OpenSSH client (full ~/.ssh/config
  fidelity: aliases, ProxyJump, agent, Match) — no paramiko/asyncssh.
- `run(script, *, stdin_data, timeout_s)` → ExecResult; command+env delivered
  via stdin script; client-side timeout kill (`proc.kill()` on expiry).
- `read_bytes(path)` / `write_bytes(path, data)` via `cat` over the channel
  (binary-safe, no SFTP subsystem dependency).
- `probe()` (cached per connection): remote `$HOME`, `git --version` presence,
  `uname` — consumed by later PRs.
- Tests: `tests/test_ssh_transport.py` (pure: script construction, secrets
  absent from argv, timeout math) + `tests/e2e_ssh_transport.py` (real sshd in
  Docker: run/read/write/binary round-trip/timeout/reconnect-after-drop;
  skips gracefully when Docker is absent).

## PR 2 — `feat/ssh-backend`: the backend

New `agents/adk_cc/sandbox/backends/ssh_backend.py` implementing the full
`SandboxBackend` ABC over `SshTransport`.

- `exec` / `exec_stream` (真 streaming from the subprocess pipes, chunked
  ExecChunks, terminal `result` chunk), `read_text/bytes`, `write_text/bytes`,
  `ensure_workspace` (`mkdir -p` + probe), `container_cwd` = identity,
  `close()` drops the control connection.
- Allow-path enforcement client-side before HTTP/exec (NoopBackend-style
  string checks against fs configs) — same fail-fast contract as Daytona's
  `_check_allowed`.
- `_runtime_env()` merged per exec via the stdin script (respects the
  TTL-cached user-over-tenant resolution; values never on argv).
- `WorkspaceRoot`: add `remote: bool = False` — `__post_init__` skips the
  LOCAL `os.path.realpath` canonicalization when set (workspace.py:69 would
  otherwise mangle/collide remote paths against the local fs).
- Registration: `make_default_backend` branch `name == "ssh"` reading
  `ADK_CC_SSH_HOST` + `ADK_CC_SSH_WORKSPACE_PATH` (env-driven path, usable
  before any UI exists).
- Tests: unit suite w/ fake transport (`tests/test_ssh_backend.py`) +
  `tests/e2e_ssh_backend.py` — the scripted-LLM REAL runtime (same harness as
  `e2e_desktop_file_status.py`): a real turn writes/edits files on the sshd
  container's volume; assert content over the transport.

## PR 3 — `feat/session-backend-status`: per-session backend truth in the UI

Independent of PR 1-2; benefits container/host today. Land before PR 4.

- Backend: expose the session's RESOLVED backend — `GET
  /desktop/sessions/{id}/backend` (or folded into an existing session status
  payload): `{backend, detail, isolated}` read from the seeded session state
  (what `get_backend` returns), NOT from global settings.
- `SandboxBadge`: switch data source from `getSandbox()` (global config) to
  the session endpoint; render per-backend chips — "Sandboxed" (green),
  "Host" (none, as today), "SSH: <host>" (new), etc. Keep the
  applies-to-new-chats semantics in copy.
- Rule enforced/communicated: backend is immutable per session (workspace
  paths, panel routing, checkpoint store are keyed to it); switching = new
  chat.
- Tests: route test + Playwright UI e2e asserting the badge reflects a
  per-session override (seeded via a test factory), not the global setting.

## PR 4 — `feat/remote-projects`: desktop integration + permission floor

The feature becomes user-visible. Depends on PR 2 (+3 for the badge).

- Project registry: `{id, name, remote: {host, path}}` alongside `repo_path`;
  `session_workspace_path` & the tenant resolver understand remote entries.
- Tenancy: desktop `backend_factory` branches — remote project → `SshBackend`
  (per-session instance over the shared per-host transport) + remote-flagged
  `WorkspaceRoot`; local projects unchanged. (The seam exists:
  `TenancyPlugin(backend_factory=…)`, `set_backend` at tenancy.py:220-226.)
- UI: "Add remote project" (host + absolute path + Test-connection button
  hitting a new probe route); SandboxBadge shows `SSH: <host>` via PR 3's
  endpoint; copy states the not-containerized trust level.
- **Permission floor (security-relevant, ships IN this PR, not after):**
  remote-aware `classify_path` — expanduser against the probed remote `$HOME`,
  literal matching, NO local realpath/expanduser (today's code would guard the
  local machine's `~/.ssh` while the agent reads the remote's). Best-effort
  documented: no remote realpath in v1.
- Tests: resolver unit tests; permission floor pure tests (remote-home
  expansion, `~/.ssh` deny on remote paths); e2e: real sshd + real runtime,
  full turn against a registered remote project; UI e2e for add-project flow.

## PR 5 — `feat/remote-file-panel`: file tree, viewer, git markers over SSH

Depends on PR 4. Extends `feat/file-tree-git-status` (0e61384 — merge first).

- `desktop_files.py`: for remote projects route tree (`ls -1Ap` per dir),
  read (`cat`, 1 MiB cap preserved), and status (`git -C <path> status
  --porcelain -z --untracked-files=all --no-renames`) through the SAME
  `SshTransport` connection. Local projects: unchanged code path.
- Same coarse-status mapping + `is_repo=false` degradation; timeouts bounded.
- Tests: route tests against the sshd container (modified/new/subdir/clean/
  non-repo, mirroring `test_desktop_files_status.py`); e2e: real turn on a
  remote project → markers appear.

## PR 6 — `feat/remote-checkpoint`: undo net + resilience

Depends on PR 5. The safety net remote in-place edits deserve.

- Remote shadow git when the probe found git: shadow store under remote
  `~/.adk-cc/checkpoints/<project>/<session>` driven via `git --git-dir
  --work-tree` over the transport (env-style GIT_DIR doesn't survive ssh
  cleanly; flags do). Same snapshot/restore/log semantics as
  `desktop_checkpoint.py`; the user's remote `.git` untouched (same
  guarantees, asserted in e2e).
- No git on remote → checkpoints DISABLED with a visible badge/tooltip in the
  panel — never silently.
- Transport resilience: reconnect-with-backoff on dropped control connection
  (reuse the Daytona backoff shape: bounded attempts + jitter), "reconnecting…"
  surfaced in the panel; in-flight execs fail with a retryable error.
- Tests: e2e on the sshd container — snapshot → agent edit → undo restores;
  kill the control master mid-session → next op reconnects; no-git remote →
  disabled badge visible.

## Sequencing

```
PR 1 ──► PR 2 ──► PR 4 ──► PR 5 ──► PR 6
              ▲
PR 3 ─────────┘   (independent; land any time before PR 4)
```

Also merge first: `feat/file-tree-git-status` (0e61384, done & verified) —
PR 5 builds on it. Optional side-spike (no PR): `docker[ssh]` +
`ADK_CC_DOCKER_HOST=ssh://…` for remotes that have Docker — validates the
remote story and gives containerized remote exec, but doesn't cover
panel/checkpoint, so the main track is unaffected.

## Risks / open items

- ssh_config edge cases → mitigated by using the system client exclusively.
- High-RTT links: multiplexing hides handshakes but not per-op RTT; panel ops
  are batched (one status call per refresh) — acceptable v1, measure later.
- Windows remotes: out of scope v1 (POSIX tools assumed); detect via probe and
  fail with a clear message.
- Remote realpath for the permission floor deferred (documented best-effort);
  revisit with a batched `readlink -f` if it proves worth the RTT.
