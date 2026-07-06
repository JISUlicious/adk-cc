# adk-cc desktop — self-contained Linux AppImage

One file that runs the desktop app on an x86_64 Linux machine with **nothing
pre-installed** — no Python, pip, Node, Rust, or system WebKit. Everything is
bundled: the Tauri window, a relocatable Python with the backend deps, the
`agents/` source, the built frontend, and the GTK/WebKit runtime.

## Build

Needs Docker (on an Apple-Silicon Mac it builds x86_64 under emulation — slower
but correct):

```
./scripts/build-appimage.sh
# → dist/adk-cc-x86_64.AppImage
```

Target a different arch with `ADK_CC_APPIMAGE_PLATFORM=linux/arm64`. First build
pulls the toolchain (slow); later builds reuse cached layers.

## Install & run (on the Linux machine)

```
chmod +x adk-cc-x86_64.AppImage
./adk-cc-x86_64.AppImage
```

On first launch it creates `~/.adk-cc-desktop/settings.env`. Put your model API
key/endpoint there and relaunch:

```
# ~/.adk-cc-desktop/settings.env
ADK_CC_API_KEY=sk-...
ADK_CC_API_BASE=https://integrate.api.nvidia.com/v1
ADK_CC_MODEL=openai/z-ai/glm-5.1
```

The app starts without a key (the UI loads), but model calls fail until it's
set. All state lives under `~/.adk-cc-desktop/` (sessions, worktrees, secrets).

Requires the model endpoint to be reachable from that machine.

## How it works

`main.rs` spawns the bundled Python (`$APPDIR/usr/lib/adk-cc/python`) running
`uvicorn adk_cc.service.server:make_app` on `127.0.0.1:8765`, then points the
window at it. Paths resolve from `$APPDIR` when packaged, or the dev repo
otherwise (`resolve_layout`).

## Build notes (Docker + emulation)

- AppImage-format tools can't self-mount under Docker's x86_64 emulation, so
  `linuxdeploy` is extracted with `unsquashfs` and the final AppImage is packed
  by hand (`mksquashfs` + a prepended type-2 runtime) — no `appimagetool`.
- The Python payload is added **after** `linuxdeploy` so it doesn't scan the
  bundled `.so` files (e.g. `_tkinter` → `libtcl`, which we don't ship).
- Deps install into the bundled standalone Python's own pip (uv won't modify a
  managed Python); `--break-system-packages` overrides its PEP-668 marker.

## Verified

Built + backend smoke-tested in a clean x86_64 container: the bundled Python
boots the backend and serves `/list-apps` with no system Python/pip. The GTK/
WebKit libs are bundled. The GUI window itself must be verified on a real
x86_64 Linux desktop (a headless container can't exercise the webview).
