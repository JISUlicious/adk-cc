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

## Which UI am I looking at?

**The most common confusion, so read this first.** adk-cc has two UI shells, and
which one you get is decided by **which bundle is served**, not by how you
started the backend:

| Shell | Built from | Looks like |
|---|---|---|
| **Desktop** | `web/dist-desktop` (`npm --prefix web run build:desktop`) | left rail of **projects**, per-project sessions, Files panel with the tree, model chip |
| **Web** | `web/dist` (`npm --prefix web run build`) | plain chat, sign-in, artifacts panel — no projects rail, no file tree |

`VITE_ADK_CC_DESKTOP=1` is baked into `dist-desktop` at **build** time. The
backend's `ADK_CC_DESKTOP=1` is a **runtime** setting that turns on desktop
routes, the local artifact store and the project registry — it does not change
which bundle gets served. Two independent switches.

**If you see the web UI when you expected the desktop one**, one of these is true:

1. `web/dist-desktop` was never built. `npm --prefix web run build` builds only
   the web bundle. Run `npm --prefix web run build:desktop`.
2. You pointed `ADK_CC_UI_DIST` at `web/dist`. It overrides everything.
3. You are on the Vite dev server (`npm --prefix web run dev`, port 5173) —
   that serves the web shell unless you set `VITE_ADK_CC_DESKTOP=1`.
4. The bundle is stale: shared UI code changed and only one of the two bundles
   was rebuilt. They are separate builds of the same source.

As of 2026-07-29 the backend picks `web/dist-desktop` automatically when
`ADK_CC_DESKTOP=1` and that directory exists, and logs a warning naming the fix
when it does not — you no longer have to set `ADK_CC_UI_DIST` by hand. Check
which one you got:

```bash
# 1. what the server decided (look for the warning too)
grep -i "dist-desktop\|serving the WEB UI" <server log>

# 2. compare the page you were served against the bundles on disk
curl -s http://127.0.0.1:8000/ > /tmp/served.html
diff -q /tmp/served.html web/dist-desktop/index.html && echo "desktop bundle"
diff -q /tmp/served.html web/dist/index.html         && echo "web bundle"
```

## Running in dev (from the repo)

Needs the Python env (`uv sync`) and the desktop frontend built.

**Native window** — `tauri-cli` required (`cargo install tauri-cli`):

```
cd src-tauri && cargo tauri dev     # beforeDevCommand builds dist-desktop;
                                    # main.rs spawns the backend from repo/.venv
```

**Server-only** (quick check, no native window — open a browser at the port).
This is the path where people used to land on the web UI by accident:

```
npm --prefix web run build:desktop        # REQUIRED — this builds the shell
ADK_CC_DESKTOP=1 ADK_CC_ALLOW_NO_AUTH=1 ADK_CC_SERVE_UI=1 \
  ADK_CC_AGENTS_DIR="$PWD/agents" ADK_CC_SANDBOX_BACKEND=noop \
  .venv/bin/uvicorn adk_cc.service.server:make_app --factory --port 8000
# → http://127.0.0.1:8000   (serves web/dist-desktop automatically)
```

`ADK_CC_UI_DIST` is only needed to serve a bundle from somewhere else — setting
it to `web/dist` is what produced the web shell in a desktop session.

If the window/page loads but the projects rail is missing, rebuild the bundle
(`npm --prefix web run build:desktop`) rather than restarting the backend: the
shell is in the bundle, not in the server.

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
