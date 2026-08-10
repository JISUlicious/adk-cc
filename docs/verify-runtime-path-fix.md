# Verifying the runtime-path fix (remote operator guide)

What this checks: the model is deliberately taught RUNTIME paths
(`/workspace/…` under Docker), and two layers used to reject that spelling
for files INSIDE the project:

- layer 1 — `read denied by fs_read: /workspace/...` (fixed in `229a40d`)
- layer 2 — confirmation card *"read_file targets a path outside the project
  scope"* (fixed in `0fca05f`)

Background: `analysis/path-and-workspace-audit.md`. Only docker / daytona /
sandbox_service sessions exercise this — SSH workspaces are identity-mapped
by design, so the fix is a no-op there.

## 0. Deploy

```bash
git pull && <restart server>     # Python-only; no web rebuild needed
```

Confirm the build took — the boot banner is new in the same series:

```bash
grep "resolved roots:" server.log | tail -1
```

No banner line → pre-fix code; stop and fix the deployment first.

## 1. The exact failing scenario (2 minutes, in the UI)

In a Docker-backend session, send:

> Call the read_file tool with path="/workspace/<an existing project file>"
> — use exactly that absolute path, do not substitute another. Then repeat
> its first line verbatim.

| you see | meaning |
|---|---|
| file content, no card | ✅ both layers fixed |
| `read denied by fs_read: /workspace/...` | ❌ layer 1 not deployed |
| card: "targets a path outside the project scope" | ❌ layer 2 not deployed |

## 2. The organic reproduction (what actually bit in production)

Run a skill script that fails (a missing dependency is enough), then ask:

> Read the script that failed and show me line 40.

The traceback names `/workspace/.adk-cc/skill-runtime/...`; the model hands
that spelling to `read_file`. Pre-fix this was the reported error verbatim;
post-fix it just reads.

## 3. Proof the crossing fired (not just absence of error)

The rewrite logs at DEBUG:

```bash
grep "resolve: runtime path" server.log | tail -5
# resolve: runtime path /workspace/... -> host /srv/.../...
```

At INFO log level this line will not appear — its absence is then NOT
evidence either way; rely on §1's behavior.

## 4. Negative checks (the fix must not widen access)

Ask the agent to read, in the same session:

- `/workspace-evil/x` → must NOT map into the workspace (component-boundary
  prefix match) — expect a normal not-found/denial.
- `/etc/passwd` → still denied/gated exactly as before. The crossing only
  re-spells paths INTO the workspace; it grants nothing new — the existing
  allow-list and protected-path floor judge the result unchanged.

## 5. Scripted (needs Docker + a live model key on the box)

```bash
ADK_CC_LIVE=1 .venv/bin/python tests/e2e_runtime_path_live.py    # expect 4/4
```

Real server, real model, real DockerBackend: the model calls
`read_file("/workspace/notes.txt")`; asserts no denial, no scope card, and
the file's content in the reply.

## If it fails

Report three things:

1. which row of the §1 table you hit (names the broken layer),
2. the `resolved roots:` banner line (names the deployment/config),
3. any `resolve: runtime path` lines (says whether the crossing ran at all).
