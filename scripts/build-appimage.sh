#!/usr/bin/env bash
# Build the self-contained adk-cc desktop AppImage (Linux x86_64) via Docker.
# Run from any machine with Docker; the output lands in ./dist/.
#
#   ./scripts/build-appimage.sh
#
# First run pulls the toolchain image (rust/node/uv/webkit) — slow; later runs
# reuse the cached layers and only rebuild the app.
set -euo pipefail
cd "$(dirname "$0")/.."

IMG=adk-cc-appimage
# Target arch of the Linux machine that will run the AppImage. Default x86_64;
# on an Apple-Silicon host this builds under emulation (slower but correct).
# Override for an ARM Linux target: ADK_CC_APPIMAGE_PLATFORM=linux/arm64
PLATFORM="${ADK_CC_APPIMAGE_PLATFORM:-linux/amd64}"
mkdir -p dist

echo ">> docker build for $PLATFORM (toolchain layers cache after the first run)…"
docker build --platform "$PLATFORM" -t "$IMG" -f packaging/appimage/Dockerfile .

echo ">> extracting the AppImage from the image…"
cid=$(docker create "$IMG")
docker cp "$cid:/out/." dist/
docker rm "$cid" >/dev/null
chmod +x dist/*.AppImage 2>/dev/null || true

echo ">> done:"
ls -la dist/*.AppImage
