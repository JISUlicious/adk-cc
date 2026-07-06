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
mkdir -p dist

echo ">> docker build (toolchain layers cache after the first run)…"
docker build -t "$IMG" -f packaging/appimage/Dockerfile .

echo ">> extracting the AppImage from the image…"
cid=$(docker create "$IMG")
docker cp "$cid:/out/." dist/
docker rm "$cid" >/dev/null
chmod +x dist/*.AppImage 2>/dev/null || true

echo ">> done:"
ls -la dist/*.AppImage
