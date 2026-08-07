#!/usr/bin/env bash
# Why can't the adk-cc sandbox reach PyPI when other containers can?
#
# Run ON THE SANDBOX HOST (the machine whose Docker daemon adk-cc talks to).
# It inspects the live sandbox container, then runs the same checks inside it
# and inside a PLAIN container on the default network, side by side — because
# "other containers work" is the useful comparison, and what differs is nearly
# always this backend's config rather than the network itself.
#
#   ./scripts/diagnose-sandbox-network.sh [container-name]
#
# With no argument it picks the first container named adk-cc-*.
set -uo pipefail

IMAGE="${ADK_CC_SANDBOX_IMAGE:-adk-cc-sandbox:latest}"
NAME="${1:-$(docker ps -a --filter 'name=adk-cc-' --format '{{.Names}}' | head -1)}"
PROBE="$(mktemp)"
trap 'rm -f "$PROBE"' EXIT

# Fed to `bash -s` on stdin rather than embedded as a quoted argument: nesting
# a script inside a quoted string mangles every inner quote, and the first two
# versions of this file printed nonsense for exactly that reason.
cat > "$PROBE" <<'PROBE_EOF'
say() { printf "    %-24s %s\n" "$1" "$2"; }

# eth0 specifically: the kernel exposes a pile of virtual devices (gre0, tunl0,
# sit0 …) in every container, so listing them all makes "no network" look like
# plenty of network.
if [ -e /sys/class/net/eth0 ]; then
  say "network attached" "yes (eth0)"
else
  say "network attached" "NO — this container is network=none"
fi
say "HTTPS_PROXY" "${HTTPS_PROXY:-<unset>}"
say "NO_PROXY" "${NO_PROXY:-<unset>}"
say "nameserver" "$(awk '/^nameserver/{print $2}' /etc/resolv.conf | tr '\n' ' ')"
say "HOME writable" "$(touch "$HOME/.probe" 2>/dev/null && echo "yes ($HOME)" || echo "NO ($HOME)")"

if getent hosts pypi.org >/dev/null 2>&1; then
  say "DNS pypi.org" "OK -> $(getent hosts pypi.org | head -1 | awk '{print $1}')"
else
  say "DNS pypi.org" "FAIL (cannot resolve)"
fi

python - <<'PY'
import socket
socket.setdefaulttimeout(8)
try:
    socket.create_connection(("pypi.org", 443))
    print("    TCP pypi.org:443         OK")
except Exception as e:
    print(f"    TCP pypi.org:443         FAIL ({type(e).__name__})")
PY

if [ -n "${HTTPS_PROXY:-}" ]; then
  python - <<'PY'
import os, socket
from urllib.parse import urlparse
u = urlparse(os.environ["HTTPS_PROXY"])
host, port = u.hostname, u.port or 8080
socket.setdefaulttimeout(8)
try:
    socket.create_connection((host, port))
    print(f"    TCP proxy                OK ({host}:{port})")
except Exception as e:
    print(f"    TCP proxy                FAIL ({host}:{port} {type(e).__name__})")
PY
fi

if command -v uv >/dev/null 2>&1; then
  # UV_CACHE_DIR points at /tmp deliberately: a read-only ~/.cache is a
  # DIFFERENT bug, and left alone it masquerades as a network failure here.
  if out=$(UV_CACHE_DIR=/tmp/uv-diag uv pip install --dry-run \
             --python "$(command -v python)" tabulate 2>&1); then
    say "uv reaches the index" "OK"
  else
    say "uv reaches the index" "FAIL: $(echo "$out" | grep -iE 'caused by|error' | tail -1 | sed 's/^ *//')"
  fi
else
  say "uv" "NOT INSTALLED (rebuild the image)"
fi
PROBE_EOF

echo "=================================================================="
if [ -n "$NAME" ]; then
  echo "SANDBOX CONTAINER: $NAME"
  echo "  --- creation-time config (changeable only by recreating) ---"
  docker inspect "$NAME" --format '    NetworkMode              {{.HostConfig.NetworkMode}}
    ReadonlyRootfs           {{.HostConfig.ReadonlyRootfs}}
    Dns                      {{if .HostConfig.Dns}}{{.HostConfig.Dns}}{{else}}(inherits the host){{end}}
    Image                    {{.Config.Image}}' 2>/dev/null
  echo "  --- from inside ---"
  docker exec -i "$NAME" bash -s < "$PROBE" 2>&1
  echo
else
  echo "No adk-cc-* container found — start a session first, or pass a name."
  echo
fi

echo "CONTROL: same image, plain container, default network"
docker run --rm -i --network bridge "$IMAGE" bash -s < "$PROBE" 2>&1
echo "=================================================================="
cat <<'EOF'

Reading it:
  NetworkMode=none           No network at all, and this is the DEFAULT for
                             the docker backend. Set ADK_CC_SANDBOX_NETWORK=1
                             and restart the server; the container is
                             recreated automatically on the config change.
  DNS FAIL, no proxy         uv/pip are trying to reach PyPI directly. On a
                             restricted network they must go via the proxy —
                             set it with ADK_CC_SANDBOX_ENV, NOT with `export`
                             in run_bash: every command is a separate exec, so
                             an export never survives to the next one.
  DNS FAIL, proxy set        Harmless for pip/uv: with a proxy they send
                             CONNECT and never resolve pypi.org themselves.
                             Read the "TCP proxy" line instead.
  DNS OK but TCP FAIL        Egress is filtered. Use the proxy, or an internal
                             index via PIP_INDEX_URL / UV_INDEX_URL.
  HOME writable = NO         Stale container from before the HOME fix. Restart
                             the server (it recreates) or `docker rm -f` it.
  control OK, sandbox not    The difference is config, not the network.
                             Compare NetworkMode and the proxy lines above.
EOF
