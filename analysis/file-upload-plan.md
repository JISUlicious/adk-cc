# File upload: investigation + implementation plan

Status: PLAN (2026-08-10). Deep investigation done against the working tree;
every claim cites file:line. Nothing here is implemented yet.

## 0. The one-sentence design

An upload is **a file in the session's workspace** — `uploads/<name>` under
`ws.abs_path` — delivered by the same per-backend write primitives the agent
already uses, announced to the model as plain text in the next message; blobs
never enter session events, and the model needs no new concepts (it reads the
file with the fs tools it already has).

Why this shape and not the two obvious alternatives:

- **Inline `inline_data` parts in the message** — already *possible* today
  (`turn_routes.py:61-68` validates full `types.Content`; nothing strips
  parts) and LiteLlm would render images/PDFs
  (`google/adk/models/lite_llm.py:985-1132`). Rejected as the primary path:
  the base64 blob is persisted into every session JSONL rewrite
  (`file_session_service.py:450`), is invisible to the context guard
  (`context_guard.py:420-434` counts text/calls only) and to microcompact
  (`microcompact.py:120-133`), and the chatgpt-codex backend silently drops
  such parts (`models/chatgpt_codex.py:163-183`). A 50 MB CSV can't go to a
  model anyway — the agent should *analyze* it, not read it into context.
- **Artifacts as the transport** — the POST route exists
  (`adk_web_server.py:1735`), a frontend client exists but is unmounted
  (`artifacts.ts:178-196`, dead `ArtifactsPanel.tsx`), and
  `load_artifact_to_sandbox` was explicitly designed for "a user upload"
  (`tools/load_artifact_to_sandbox.py:3-6`). Rejected as the primary path:
  it is two hops (the model must remember to call a tool before it can read
  the file), it hard-refuses under the noop backend which is the desktop
  default (`load_artifact_to_sandbox.py:64-73`, `src-tauri/src/main.rs:384`),
  and a client-side artifact POST emits no event, so nothing appears in the
  thread (`ArtifactChip` is driven by `actions.artifactDelta` only).
  Artifacts stay what they are today: agent *output*.

## 1. What exists today (survey results)

### 1.1 Where the workspace physically lives, per mode/backend

| Backend | Workspace location | Binary-safe server→workspace write today |
|---|---|---|
| noop (desktop default) | host dir = `ws.abs_path` (`noop_backend.py:127-128`) | plain host fs (datasets does exactly this: `datasets.py:134-143`) |
| local_container | host dir, bind-mounted at the identical path (`local_container_backend.py:120-124`) | plain host fs — mount makes it visible instantly |
| ssh remote | remote dir, identical-path model (`ssh_backend.py:6-8`) | `transport.write_file` — bytes over stdin, binary-safe (`ssh_transport.py:394-413`) |
| docker (web) | dir on the **daemon host**, mounted at `/workspace` (`docker_backend.py:297-302`) | **NONE** — only `write_text` (in-memory tar → `put_archive` `:484-516`, base64-exec fallback on read-only 400 `:517-543`); base `write_bytes` utf-8-decodes and corrupts/raises on binary (`base.py:219-233`) |
| daytona | remote sandbox fs (`daytona_backend.py:225`) | `write_bytes` = multipart POST to the toolbox proxy, binary-safe (`:987-1022`) |
| sandbox_service | remote session volume at `/workspace` (`sandbox_service_backend.py:91`) | **NONE** — `write_text` posts octet-stream (`:624-654`) but no `write_bytes` override |

`WorkspaceRoot.abs_path` derivation: desktop in-place project root
(`desktop_workspace.py:138-145`), desktop remote = remote path + SshBackend
(`:127-137`, `:166-204`), web = `<root>/<tenant>/<user>/` (+ per-session
scratch) via `TenantContext.workspace()` (`tenancy.py:52-79`).

Two traps that shape the design:

- **ensure ordering**: `backend.ensure_workspace(ws)` normally runs at the
  first tool call (`tenancy.py:290-315`). An upload can arrive before any
  turn; on docker/daytona that raises ("no workspace path set" /
  "used before ensure_workspace()"). The upload path must call
  `ensure_workspace` itself (it is idempotent and retried already).
- **uid/ownership** (docker): container runs `1000:1000`, workspace is
  chown'd to that uid by a helper container (`docker_backend.py:384-388`).
  Host-side writes on the daemon machine are not an option anyway (the
  daemon may be remote); all docker delivery goes through the daemon API.

### 1.2 Server route patterns to copy

- All existing uploads are **raw body + explicit Content-Type**, not
  multipart (skills: `desktop_settings.py:373`, `identity_routes.py:652`,
  `admin_routes.py:204`; datasets: `desktop_files.py:519`). Multipart is
  installed but unused. Stay with raw-body for consistency.
- Validation to reuse from datasets: `check_name`
  (`datasets.py:63-80`, single component, no `/` `\` leading-dot),
  `check_size` (`:92-100`), atomic `.part` + `os.replace` write
  (`:134-143`), size env knob pattern (`ADK_CC_DATASET_UPLOAD_MAX_MB`).
- Binding patterns: desktop = `?project_id=&session_id=` validated by
  `_resolve_within` (`desktop_files.py:42-69`, resolve+prefix escape check);
  web = auth middleware principal (`auth.py:340-405`) + `_require_auth`/
  `_safe_id` (`identity_routes.py:34-38`) scoping dirs by
  `auth.tenant_id`/`auth.user_id`.
- Known gap this plan must NOT widen: dataset routes hard-refuse remote and
  containerized workspaces (`desktop_files.py:443-470` → 409s; task #75).
  The upload helper is the machinery that later un-blocks #75.

### 1.3 Frontend

- `Composer` is string-only (`onSend(text)`, `Composer.tsx:44`), has slots
  (`footer`/`taskStrip`/`modelChip`) and no file input / drag-drop / paste
  handling. Anchor points: input row at `:191`, wrapper `:143` (slash menu
  already overlays there — same pattern for a drop overlay).
- Hidden-`<input type=file>` + api-client upload is an established idiom
  (4 components; e.g. `ArtifactsPanel.tsx:156-174`,
  `DesktopSettingsSections.tsx:387`).
- `apiFetch` handles raw-blob bodies with explicit Content-Type and replays
  them safely on 401 refresh (`client.ts:103-135`).
- `PUT /desktop/datasets/{name}` exists server-side with **no client
  caller** — evidence that a raw-body file PUT is deploy-ready plumbing.

## 2. Design

### 2.1 Core: one delivery helper, two mounts

New module `agents/adk_cc/service/uploads.py`:

```
deliver_upload(ws: WorkspaceRoot, backend, name: str, data: bytes,
               overwrite: bool = False) -> dict
```

1. `check_upload_name(name)` — datasets' `check_name` rules (single path
   component; no extension allow-list: unlike datasets, this IS the general
   endpoint, and safety comes from the fixed destination, not the suffix).
2. Destination = `uploads/<name>` under `ws.abs_path` (posix-joined for
   remote roots). `uploads/` is a fixed, created-if-missing subdir — a
   sanitized single-component name cannot escape it or touch workspace
   config/dotfiles.
3. `backend.ensure_workspace(ws)` first (idempotent; fixes the
   before-first-turn ordering trap).
4. Delivery dispatch:
   - workspace is host-local (noop, local_container, and desktop scratch):
     atomic host write, datasets-style (`.part` + `os.replace`).
   - otherwise: `backend.write_bytes(dest, data, fs_write=ws.fs_write_config())`
     — the exact call `load_artifact_to_sandbox` already makes
     (`load_artifact_to_sandbox.py:156-158`).
5. Collision policy: `overwrite=false` → 409 with the existing file's size;
   the UI offers replace / auto-rename (`name-2.ext`).
6. Returns `{name, rel_path: "uploads/<name>", bytes, workspace}` — rel
   path only; host paths never reach the client (same rule as
   `display_path`, `tools/_fs.py:23-45`).

Routes (thin wrappers over the helper):

- **Desktop**: `PUT /desktop/uploads/{name}?project_id=&session_id=` in
  `desktop_files.py` — binding via the existing `_root(request)` +
  workspace/backends resolution the profile route already demonstrates
  (`desktop_files.py:576-594` hand-builds ws + `resolved_session_backend`).
  Crucially this route does NOT go through `_workspace_is_local` refusal —
  delivery handles remote/containerized via the backend.
- **Web**: `PUT /api/uploads/{name}?session_id=` mounted in `server.py`,
  principal from `request.state.adk_cc_auth`, workspace via the same
  resolver `TenancyPlugin` seeds from (share the function, don't re-derive
  the layout) — so the file lands in exactly the root the agent's tools see.

Size cap: `ADK_CC_UPLOAD_MAX_MB` (default 100). Enforced from
`Content-Length` BEFORE reading the body where present, and again after
(the skills routes check only after buffering — copy the datasets 413 but
add the header pre-check; a 500 MB default cap enforced after a 500 MB
allocation is a known wart, `desktop_files.py:519` + `datasets.py:136`).

### 2.2 Backend gap-closing (prerequisite, small)

- `DockerBackend.write_bytes` — same shape as `write_text` but tar the raw
  bytes (`put_archive` is already binary-capable; only the *text* wrapper
  forces utf-8). The read-only-rootfs fallback base64-encodes, which is
  binary-safe by construction; keep the existing "read-only in message"
  trigger (`docker_backend.py:517-543`). Note the argv-length ceiling on the
  fallback: chunk the b64 payload into ~64 KB appends (`>>`) when large.
- `SandboxServiceBackend.write_bytes` — the octet-stream POST already
  carries raw bytes; add the override that skips the utf-8 encode.
- `BaseBackend.write_bytes` — keep utf-8 delegation but raise a CLEAR
  error naming the backend when `data` is not valid utf-8, so a missing
  override fails loudly instead of corrupting (today it raises a bare
  `UnicodeDecodeError` from `base.py:219-233`).

ssh and daytona already have binary-safe `write_bytes` — no change.

### 2.3 Telling the model (no new model-side machinery)

On send, the client prepends nothing and invents no part types: it appends
one plain-text line to the user message per staged file —

```
[attached file: uploads/data.csv — 2.3 MB]
```

The model reads it with `read_file`/`run_bash`/skills like any workspace
file. This works identically on every model backend, including
chatgpt-codex (which would silently drop blob parts,
`chatgpt_codex.py:163-183`), costs zero context beyond the one line, and
needs no changes to turns/broker/renderers. The runtime-path crossing
(`_fs.resolve` → `to_host_path`) already handles the model spelling the
path either way.

Deliberately deferred (P3): true multimodal image input via ADK's
`SaveFilesAsArtifactsPlugin` (exists, unregistered —
`save_files_as_artifacts_plugin.py:35-48`) which swaps blobs out of the
stored message for artifact references — the correct mitigation for the
session-store/context problems if we ever want images to reach the model
directly.

### 2.4 UI

`Composer`:
- paperclip button (hidden `<input type=file multiple>`, the 4-component
  idiom) + drag-drop overlay on the wrapper (`:143`) + paste-file handler;
- staged-attachment chips above the input row (name, size, remove ×);
- `onSend` widened to `(text, attachments: File[])` — ChatPage sequences:
  upload each (progress on the chip) → all landed → send the message with
  the appended attachment lines. Failed upload = red chip + message NOT
  sent (no half-truth messages naming files that aren't there).

`FileTreeSidePanel` (desktop): refresh after a successful upload so
`uploads/` appears immediately.

### 2.5 Hardening rider (independent, cheap)

`turn_routes._content` accepts arbitrary `inline_data` today with no size
guard — any client can bomb the session store through `/api/turns`
(`turn_routes.py:61-68` + `file_session_service.py:450`). Add a reject (413)
for messages whose serialized parts exceed ~1 MB unless/until P3 lands the
artifact-swap plugin. This closes the hole the surveys exposed regardless
of the upload feature.

## 3. Mode × backend matrix (what P0+P1 must prove)

| Mode | Backend | Delivery path | Test tier |
|---|---|---|---|
| desktop project | noop | host atomic write | unit + live UI e2e |
| desktop project | local_container | host write (bind mount) | unit (mount visibility via run_bash in docker e2e tier) |
| desktop remote | ssh | `transport.write_file` | unit w/ fake transport; live vs LAN box opt-in |
| web single/multi | docker | `write_bytes` (new) via daemon | e2e with real Docker (extend `e2e_docker_backend_runtime.py`) |
| web | daytona | existing multipart upload | opt-in live (`ADK_CC_LIVE`) |
| web | sandbox_service | `write_bytes` (new) | unit w/ fake service |
| any, pre-first-turn | docker/daytona | `ensure_workspace` inside helper | unit: upload to a fresh session, then first turn reads it |

## 4. Phases

**P0 — server core.** `uploads.py` helper + name/size validation +
`DockerBackend.write_bytes` + `SandboxServiceBackend.write_bytes` + loud
base fallback + both route mounts. Tests FIRST (this week's rule): unit
suite with per-backend fakes + the ensure-ordering case + escape attempts
(`../x`, absolute, dotfile) + overwrite policy; extend
`e2e_docker_backend_runtime.py` with a binary round-trip (write via helper,
`sha256sum` inside the container via exec).

**P1 — composer UI + live proof.** Attach button/drag-drop/chips, upload →
send sequencing, attachment lines, FileTree refresh. Live UI e2e (the
`e2e_skill_activity_ui.py` harness pattern): stage a CSV, send "what's in
the attached file?", assert the model reads `uploads/…` and answers; A/B by
asserting the upload chip + the `uploads/` tree entry exist.

**P2 — remote/container acceptance + #75 convergence.** SSH live check
(skip-gracefully), daytona opt-in live, docker web e2e. Then point the
dataset routes' refusal (`_workspace_is_local`) at the same delivery
helper — datasets on remote/containerized workspaces stop 409ing (task
#75) by reusing, not duplicating, the upload path.

**P3 — deferred, decide later.** (a) images to the model via
`SaveFilesAsArtifactsPlugin` + artifact-ref rendering; (b) surfacing
artifact upload in `ArtifactsSidePanel` (the dead `ArtifactsPanel` code is
a donor); (c) paste-image → file staging; (d) per-tenant storage quotas on
`uploads/` (web).

## 5. Explicit non-goals

- No multipart parsing (repo convention is raw body; nothing needs
  streams below the 100 MB cap).
- No blobs in session events (P3's plugin is the only sanctioned route).
- No new model tool: `uploads/` + fs tools is the whole contract.
- No upload-to-artifact-store as primary transport (artifacts remain
  agent output; the desktop noop refusal makes them a dead end for the
  main flow anyway).

## 6. Risks

- **Memory buffering**: body is buffered once (raw-body convention) and
  docker/daytona/ssh each buffer once more in their client call. At the
  100 MB default cap that is acceptable; raising the cap later means
  revisiting with chunked delivery (daytona multipart + ssh stdin can
  stream; docker put_archive can take a streamed tar).
- **Container recreation races**: local_container recreates on mount-set
  change, docker on config-signature change — an upload mid-recreate hits
  the same lock the cross-loop fix (13e3f5c) serializes; delivery goes
  through the same `_ensure_container` path, so no new locking.
- **Name semantics on case-insensitive host + case-sensitive container**
  (macOS desktop vs linux container): collision check must use the
  backend's view (exec `test -e`) when the workspace is not host-local.
- **Web scratch vs home**: the helper must resolve the workspace through
  the SAME function the tenancy plugin seeds from; deriving the layout
  independently is how the 25-session store mystery class of bugs happens.
