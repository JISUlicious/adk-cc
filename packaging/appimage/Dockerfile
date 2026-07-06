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
      libssl-dev patchelf desktop-file-utils zsync \
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

# AppImage tooling: appimagetool + linuxdeploy + the GTK plugin.
RUN wget -q -O /usr/local/bin/appimagetool \
      https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
 && wget -q -O /usr/local/bin/linuxdeploy \
      https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage \
 && wget -q -O /usr/local/bin/linuxdeploy-plugin-gtk \
      https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh \
 && chmod +x /usr/local/bin/appimagetool /usr/local/bin/linuxdeploy /usr/local/bin/linuxdeploy-plugin-gtk

WORKDIR /src
COPY . /src

RUN bash packaging/appimage/build-in-container.sh
