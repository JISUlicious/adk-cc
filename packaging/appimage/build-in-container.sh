#!/usr/bin/env bash
# Assemble the adk-cc AppImage inside the build container (see Dockerfile).
# Produces /out/adk-cc-*.AppImage. Not meant to run on a host directly.
set -euxo pipefail
export APPIMAGE_EXTRACT_AND_RUN=1

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
PYROOT="$(dirname "$(dirname "$PYBIN")")"   # cpython-…-linux root (self-contained)
cp -a "$PYROOT" "$BUILD/python"
uv export --no-hashes --no-emit-project --format requirements-txt > "$BUILD/req.txt"
uv pip install --python "$BUILD/python/bin/python3" -r "$BUILD/req.txt"

# ---------------------------------------------------------------------------
# 3. Tauri window binary (frontendDist=splash is embedded; the real UI is
#    served by the backend, so plain `cargo build` is enough — no tauri-cli).
# ---------------------------------------------------------------------------
cd "$REPO/src-tauri"
cargo build --release
BIN="$REPO/src-tauri/target/release/adk-cc"

# ---------------------------------------------------------------------------
# 4. Assemble the AppDir. main.rs reads $APPDIR and looks under usr/lib/adk-cc.
# ---------------------------------------------------------------------------
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$LIB"
cp "$BIN" "$APPDIR/usr/bin/adk-cc"
cp -a "$REPO/agents" "$LIB/agents"
cp -a "$REPO/web/dist-desktop" "$LIB/dist-desktop"
cp -a "$BUILD/python" "$LIB/python"
# trim bytecode caches to shrink the image
find "$LIB" -depth -type d -name '__pycache__' -exec rm -rf {} + || true
cp "$REPO/packaging/appimage/adk-cc.desktop" "$APPDIR/adk-cc.desktop"
cp "$REPO/src-tauri/icons/icon.png" "$APPDIR/adk-cc.png"

# ---------------------------------------------------------------------------
# 5. Bundle the GTK/WebKit runtime + pack the AppImage.
# ---------------------------------------------------------------------------
cd "$BUILD"
export OUTPUT="adk-cc-x86_64.AppImage"
linuxdeploy --appdir "$APPDIR" \
  --executable "$APPDIR/usr/bin/adk-cc" \
  --desktop-file "$APPDIR/adk-cc.desktop" \
  --icon-file "$APPDIR/adk-cc.png" \
  --plugin gtk \
  --output appimage
mv adk-cc*.AppImage "$OUT/"
ls -la "$OUT"
