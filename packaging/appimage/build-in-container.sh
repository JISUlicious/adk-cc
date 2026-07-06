#!/usr/bin/env bash
# Assemble the adk-cc AppImage inside the build container (see Dockerfile).
# Produces /out/adk-cc-*.AppImage. Not meant to run on a host directly.
set -euxo pipefail
export ARCH=x86_64

REPO=/src
OUT=/out
BUILD=/build
APPDIR="$BUILD/AppDir"
LIB="$APPDIR/usr/lib/adk-cc"
mkdir -p "$OUT" "$BUILD"

# ---------------------------------------------------------------------------
# 1. Frontend (desktop build → web/dist-desktop, served by the backend).
# ---------------------------------------------------------------------------
cd "$REPO/web"
npm ci
npm run build:desktop

# ---------------------------------------------------------------------------
# 2. Relocatable Python + backend deps (from uv.lock, EXCLUDING the project;
#    adk_cc itself is shipped as source + imported via PYTHONPATH).
# ---------------------------------------------------------------------------
cd "$REPO"
uv python install 3.12
PYBIN="$(uv python find 3.12)"
# readlink -f: uv exposes the interpreter via a versionless symlink
# (cpython-3.12-… → cpython-3.12.13-…). Copying the symlink would leave a
# dangling link (empty python) in the AppImage — resolve to the real dir.
PYROOT="$(readlink -f "$(dirname "$(dirname "$PYBIN")")")"
cp -a "$PYROOT" "$BUILD/python"
uv export --no-hashes --no-emit-project --format requirements-txt > "$BUILD/req.txt"
# Install deps into the bundled python's OWN site-packages via its pip. (uv
# refuses to modify a uv-managed python; the copy is a self-contained,
# relocatable python-build-standalone, so its pip installs cleanly.)
"$BUILD/python/bin/python3" -m ensurepip --upgrade 2>/dev/null || true
# --break-system-packages: the standalone python ships an EXTERNALLY-MANAGED
# marker (PEP 668); this IS our private bundle to populate, so override it.
"$BUILD/python/bin/python3" -m pip install --no-input --no-warn-script-location \
  --break-system-packages -r "$BUILD/req.txt"

# ---------------------------------------------------------------------------
# 3. Tauri window binary (frontendDist=splash is embedded; the real UI is
#    served by the backend, so plain `cargo build` is enough — no tauri-cli).
# ---------------------------------------------------------------------------
cd "$REPO/src-tauri"
cargo build --release
# Plain `cargo build` names the binary after the crate (adk-cc-desktop); we
# install it as `adk-cc` in the AppDir (matches the .desktop Exec).
BIN="$REPO/src-tauri/target/release/adk-cc-desktop"

# ---------------------------------------------------------------------------
# 4. Minimal AppDir: just the Tauri binary + desktop + icon. The app payload
#    (python/agents/dist) is added AFTER linuxdeploy, so linuxdeploy doesn't
#    recursively scan the bundled python's .so files (some, e.g. _tkinter, pull
#    in libs we don't ship) and fail.
# ---------------------------------------------------------------------------
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp "$BIN" "$APPDIR/usr/bin/adk-cc"
cp "$REPO/packaging/appimage/adk-cc.desktop" "$APPDIR/adk-cc.desktop"
cp "$REPO/src-tauri/icons/icon.png" "$APPDIR/adk-cc.png"

# ---------------------------------------------------------------------------
# 5. Bundle the GTK/WebKit runtime for the Tauri binary (deploy only — no
#    packing; appimagetool can't self-mount under emulation).
# ---------------------------------------------------------------------------
cd "$BUILD"
linuxdeploy --appdir "$APPDIR" \
  --executable "$APPDIR/usr/bin/adk-cc" \
  --desktop-file "$APPDIR/adk-cc.desktop" \
  --icon-file "$APPDIR/adk-cc.png" \
  --plugin gtk

# ---------------------------------------------------------------------------
# 6. Add the app payload (after linuxdeploy). main.rs reads $APPDIR and looks
#    under usr/lib/adk-cc for python/agents/dist-desktop.
# ---------------------------------------------------------------------------
mkdir -p "$LIB"
cp -a "$REPO/agents" "$LIB/agents"
cp -a "$REPO/web/dist-desktop" "$LIB/dist-desktop"
cp -a "$BUILD/python" "$LIB/python"
find "$LIB" -depth -type d -name '__pycache__' -exec rm -rf {} + || true

# ---------------------------------------------------------------------------
# 7. Pack the AppImage by hand: mksquashfs the AppDir, then prepend the plain
#    type-2 runtime. Both are regular ELFs, so this works under emulation and
#    the result is a normal AppImage that runs natively on the x86_64 target.
# ---------------------------------------------------------------------------
mksquashfs "$APPDIR" "$BUILD/app.sqfs" -root-owned -noappend -comp gzip -b 1M
cat /opt/runtime-x86_64 "$BUILD/app.sqfs" > "$OUT/adk-cc-x86_64.AppImage"
chmod +x "$OUT/adk-cc-x86_64.AppImage"
ls -la "$OUT"
