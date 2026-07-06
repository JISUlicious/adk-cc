# 08 — Desktop app

Read in: **English** · [한국어](./08-desktop-app.ko.md)

A single-user **local desktop app** (Tauri): no login, no server to operate. The
native window runs the Python backend as a sidecar and points itself at the
backend-served UI.

## What it is

`src-tauri/src/main.rs` spawns `uvicorn adk_cc.service.server:make_app` on
`127.0.0.1:8765` with the single-user env — no-auth, sqlite sessions,
encrypted-file secrets, `noop` sandbox (local exec), tenancy `single` — then
navigates the window from a splash to the backend URL. It's the **same** React
app as the web UI, built with `VITE_ADK_CC_DESKTOP=1` (`web/dist-desktop`); the
one difference is the right-side panel, which is a **local file tree** over the
session's git worktree instead of the web artifacts list.

## Data directory

Everything lives under `~/.adk-cc-desktop/` (override with `$ADK_CC_DESKTOP_DATA`):

```
settings.env            # user config (see below)
sessions.db             # sqlite session store
worktrees/<proj>/<sess> # per-session git worktree (the file panel's root)
secrets/                # encrypted-file credential store
credential.key          # Fernet key for the secret store
```

## Configuration — `settings.env`

On first launch the app writes a commented `settings.env` template to the data
dir. Edit it and restart. In desktop context the dotenv bootstrap
(`adk_cc/__init__.py`) loads it **first**, so it beats any repo/cwd `.env`; a
real process env var still wins over it.

```
# ~/.adk-cc-desktop/settings.env
ADK_CC_API_KEY=sk-...
ADK_CC_API_BASE=https://integrate.api.nvidia.com/v1
ADK_CC_MODEL=openai/z-ai/glm-5.1
# ADK_CC_MODEL_MAX_RPM=30      # optional
```

Resolution order: `$ADK_CC_SETTINGS_FILE`, else `$ADK_CC_DESKTOP_DATA/settings.env`,
else `~/.adk-cc-desktop/settings.env`. The app **boots without a key** (the UI
loads and logs a warning); model calls fail until a key is set.

## Running in dev (from the repo)

Needs the Python env (`uv sync`) and the desktop frontend built.

**Native window** — `tauri-cli` required (`cargo install tauri-cli`):

```
cd src-tauri && cargo tauri dev     # beforeDevCommand builds dist-desktop;
                                    # main.rs spawns the backend from repo/.venv
```

**Server-only** (quick check, no native window — open a browser at the port):

```
npm --prefix web run build:desktop
ADK_CC_DESKTOP=1 ADK_CC_ALLOW_NO_AUTH=1 ADK_CC_SERVE_UI=1 \
  ADK_CC_UI_DIST="$PWD/web/dist-desktop" ADK_CC_AGENTS_DIR="$PWD/agents" \
  ADK_CC_SANDBOX_BACKEND=noop \
  .venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8000
# → http://127.0.0.1:8000
```

## Installer — self-contained AppImage

For a machine with nothing pre-installed (no Python/pip/Node/Rust/WebKit), build
a single-file x86_64 Linux AppImage:

```
./scripts/build-appimage.sh          # → dist/adk-cc-x86_64.AppImage  (needs Docker)
```

On the target:

```
chmod +x adk-cc-x86_64.AppImage && ./adk-cc-x86_64.AppImage
```

First launch creates `~/.adk-cc-desktop/settings.env`; fill in the key and
relaunch. Requires the model endpoint to be reachable from that machine.
Build/packaging details, emulation notes, and what's bundled are in
[`packaging/appimage/README.md`](../packaging/appimage/README.md).

## How it's wired (relocatable)

`main.rs::resolve_layout()` picks paths from the app's own location:

| | packaged (AppImage) | dev (repo) |
|---|---|---|
| interpreter | `$APPDIR/usr/lib/adk-cc/python/bin/python3` | `repo/.venv/bin/python` |
| agents | `$APPDIR/usr/lib/adk-cc/agents` | `repo/agents` |
| frontend | `$APPDIR/usr/lib/adk-cc/dist-desktop` | `repo/web/dist-desktop` |

It runs `python -m uvicorn` in both; when packaged it sets `PYTHONPATH=agents`
so `adk_cc` imports from the shipped source (no pip install on the target).

## Notes

- The installer targets **x86_64 Linux**; override the build arch with
  `ADK_CC_APPIMAGE_PLATFORM=linux/arm64`.
- The agent needs to reach the configured model endpoint; there's no built-in
  model.
- Desktop mode uses the `noop` sandbox — `run_bash` and file tools operate
  directly in the session's local worktree.
