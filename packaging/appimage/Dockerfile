# Builds a self-contained adk-cc desktop AppImage (Linux x86_64).
#
# Bundles, so the target machine needs NOTHING but the .AppImage + a settings.env:
#   - the Tauri window binary
#   - a relocatable standalone Python (python-build-standalone via uv) with the
#     backend deps from uv.lock installed
#   - the agents/ backend source (imported via PYTHONPATH — no pip on target)
#   - the built dist-desktop frontend (served by the backend)
#   - the GTK/WebKit runtime (bundled by linuxdeploy-plugin-gtk)
#
# Built on Ubuntu 22.04 for a portable glibc baseline. Toolchain layers are
# cached; only the final build step re-runs when the repo changes.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
# No FUSE inside `docker build` — make appimagetool/linuxdeploy self-extract.
ENV APPIMAGE_EXTRACT_AND_RUN=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl wget ca-certificates file xz-utils build-essential pkg-config git \
      libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev \
      libssl-dev patchelf desktop-file-utils zsync squashfs-tools \
    && rm -rf /var/lib/apt/lists/*

# Node 20 (frontend build).
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Rust (Tauri binary).
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"

# uv (standalone Python + deps from uv.lock).
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# AppImage tooling. The tool AppImages can't self-mount under Docker's x86_64
# emulation ("Exec format error"), while regular ELFs run fine. So:
#   - extract linuxdeploy with unsquashfs (try each `hsqs` offset until the
#     squashfs superblock is found) and run its AppRun for GTK/WebKit bundling;
#   - grab the plain type-2 runtime binary to prepend when packing manually
#     (mksquashfs + cat) — no appimagetool needed.
RUN set -eux; cd /opt; \
    wget -q -O linuxdeploy.AppImage \
      https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage; \
    ok=0; \
    for off in $(grep -aob hsqs linuxdeploy.AppImage | cut -d: -f1); do \
      rm -rf /opt/linuxdeploy; \
      if unsquashfs -q -o "$off" -d /opt/linuxdeploy linuxdeploy.AppImage >/dev/null 2>&1; then ok=1; echo "linuxdeploy @ $off"; break; fi; \
    done; \
    [ "$ok" = 1 ] || { echo "linuxdeploy extract failed"; exit 1; }; \
    rm -f linuxdeploy.AppImage; \
    printf '#!/bin/sh\nexec "/opt/linuxdeploy/AppRun" "$@"\n' > /usr/local/bin/linuxdeploy; \
    chmod +x /usr/local/bin/linuxdeploy; \
    wget -q -O /usr/local/bin/linuxdeploy-plugin-gtk \
      https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh; \
    chmod +x /usr/local/bin/linuxdeploy-plugin-gtk; \
    wget -q -O /opt/runtime-x86_64 \
      https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64; \
    chmod +x /opt/runtime-x86_64

WORKDIR /src
COPY . /src

# Cache mounts so cargo/pip/npm don't redo everything each rebuild.
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/src/src-tauri/target \
    --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.npm \
    bash packaging/appimage/build-in-container.sh
