# Sandbox VM operator runbook

This is the one-page checklist for provisioning the sandbox host that
adk-cc's `DockerBackend` connects to. Skim end-to-end before starting.

## Topology recap

The agent process (in K8s, eventually) connects over TCP to a Docker
daemon on a separate Linux VM. The agent never runs Docker locally.
Workspaces live on the sandbox VM's filesystem; the agent reaches
them only through the `SandboxBackend` contract.

```
[agent K8s pod] ──Docker TCP API── [sandbox VM running Docker]
                                            │
                                            ├─ adk-cc-sandbox image
                                            ├─ per-session containers
                                            └─ /var/lib/adk-cc/wks/...
```

## 1. Provision the VM

- **OS**: Ubuntu 22.04 LTS or Rocky Linux 9. Other modern Linux
  distros work; these are the tested ones.
- **Hardware**: 16 physical cores, 96 GB RAM, 1 TB NVMe SSD for 100
  users (see `02-architecture.md` §5.5).
- **Network**: place on a management subnet that the agent's K8s
  namespace can reach. Block all other inbound traffic.
- **Single-purpose**: don't run other workloads on this host. The
  Docker daemon's blast radius is the host; keep the host clean.

```bash
# Ubuntu — install Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# Workspace root
mkdir -p /var/lib/adk-cc/wks
chmod 0755 /var/lib/adk-cc

# Clone adk-cc on the sandbox VM (just for the Dockerfile) and build
git clone https://github.com/JISUlicious/adk-cc.git /opt/adk-cc
cd /opt/adk-cc
docker build -t adk-cc-sandbox:latest -f Dockerfile.sandbox .
```

## 2. Pick a connection mode

### Plain TCP (simpler — for trusted internal networks)

Add `/etc/docker/daemon.json`:

```json
{
  "hosts": ["unix:///var/run/docker.sock", "tcp://10.0.0.5:2375"]
}
```

Replace `10.0.0.5` with the management-network IP.
**Don't use 0.0.0.0** unless you're certain firewall rules cover it.

```bash
systemctl edit docker
# Add (under [Service]):
#   ExecStart=
#   ExecStart=/usr/bin/dockerd
systemctl daemon-reload && systemctl restart docker
```

Configure firewall (ufw / iptables / cloud security group) to allow
only the agent's K8s NAT egress IP to reach `tcp://<vm>:2375`.

### TLS TCP (recommended for anything crossing untrusted hops)

Generate a CA, server cert, and client cert. The Docker docs at
<https://docs.docker.com/engine/security/protect-access/> are the
canonical reference. Quick version:

```bash
SANDBOX_HOST=sandbox.internal
mkdir -p ~/docker-tls && cd ~/docker-tls

# CA
openssl genrsa -aes256 -out ca-key.pem 4096
openssl req -new -x509 -days 3650 -key ca-key.pem -sha256 -out ca.pem \
  -subj "/CN=adk-cc-ca"

# Server cert
openssl genrsa -out server-key.pem 4096
openssl req -subj "/CN=$SANDBOX_HOST" -sha256 -new \
  -key server-key.pem -out server.csr
echo "subjectAltName = DNS:$SANDBOX_HOST,IP:10.0.0.5" > extfile.cnf
echo "extendedKeyUsage = serverAuth" >> extfile.cnf
openssl x509 -req -days 3650 -sha256 -in server.csr -CA ca.pem \
  -CAkey ca-key.pem -CAcreateserial -out server-cert.pem \
  -extfile extfile.cnf

# Client cert (for the agent pod)
openssl genrsa -out key.pem 4096
openssl req -subj '/CN=adk-cc-agent' -new -key key.pem -out client.csr
echo "extendedKeyUsage = clientAuth" > extfile-client.cnf
openssl x509 -req -days 3650 -sha256 -in client.csr -CA ca.pem \
  -CAkey ca-key.pem -CAcreateserial -out cert.pem \
  -extfile extfile-client.cnf
```

Configure the daemon to require mTLS:

```json
{
  "tls": true,
  "tlsverify": true,
  "tlscacert": "/etc/docker/tls/ca.pem",
  "tlscert": "/etc/docker/tls/server-cert.pem",
  "tlskey": "/etc/docker/tls/server-key.pem",
  "hosts": ["unix:///var/run/docker.sock", "tcp://10.0.0.5:2376"]
}
```

`systemctl restart docker`. Verify from the agent host:

```bash
docker --tlsverify \
  --tlscacert=ca.pem --tlscert=cert.pem --tlskey=key.pem \
  -H tcp://sandbox.internal:2376 \
  version
```

## 3. Configure the agent

Set in the agent's environment (or K8s ConfigMap / Secret for prod):

```bash
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2376
ADK_CC_DOCKER_CA_CERT=/etc/adk-cc/docker-tls/ca.pem
ADK_CC_DOCKER_CLIENT_CERT=/etc/adk-cc/docker-tls/cert.pem
ADK_CC_DOCKER_CLIENT_KEY=/etc/adk-cc/docker-tls/key.pem
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks

# Optional spawn-config tuning
ADK_CC_SANDBOX_IMAGE=adk-cc-sandbox:latest
ADK_CC_SANDBOX_MEM_LIMIT=4g
ADK_CC_SANDBOX_CPU_QUOTA=100000   # 100k = 1 CPU
ADK_CC_SANDBOX_PIDS_LIMIT=256
```

For plain TCP: drop the three `*_CERT` / `*_KEY` vars and set
`ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2375`.

## 4. Smoke test

From the agent's host (or inside the agent pod):

```bash
# Connectivity
python -c "
import docker
c = docker.DockerClient(base_url='tcp://sandbox.internal:2376',
    tls=docker.tls.TLSConfig(client_cert=('cert.pem','key.pem'),
                             ca_cert='ca.pem', verify=True))
print(c.version())
"
```

Then drive `adk api_server` against the sandbox; verify per-session
containers appear (`docker ps`) and disappear after the session ends
(`docker.close()` runs on `after_run_callback`).

## 5. Operational considerations

- **Image updates**: rebuild `adk-cc-sandbox:latest` on the sandbox
  VM after pulling new adk-cc commits. Sessions started before the
  rebuild keep using the cached layer; new sessions get the update.
- **Backup of workspaces**: `/var/lib/adk-cc/wks` is per-tenant data.
  Snapshot the volume on a schedule that matches your retention SLA.
- **Container leaks**: if the agent pod crashes mid-session, the
  per-session container may be orphaned. Run periodically:
  ```bash
  docker ps --filter label=adk-cc-session --format '{{.Names}} {{.Status}}'
  # Reap anything that's been Up >24h with no agent reachability
  ```
- **Logging**: containers don't have stdout/stderr forwarded by
  default (the model gets exec results back via the API). For
  debugging, attach: `docker logs adk-cc-<session_id>`.
- **Resource ceilings**: per-container limits are set at spawn. To
  raise a tier, set `ADK_CC_SANDBOX_MEM_LIMIT=8g` (or higher) and
  restart the agent — new sessions get the new limit.
- **Disk pressure**: Docker overlay can grow. Run
  `docker system df` and `docker system prune --volumes` on a cron.

## 6. Alternative: external sandbox service (`sandbox_service` backend)

For deployments that want to factor sandbox responsibility out of the
agent process entirely — typical for managed multi-tenant SaaS — adk-cc
ships a `SandboxServiceBackend` that talks to an external REST sandbox
service. Today's reference implementation:
[JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing).

### When to pick this over `DockerBackend`

- You don't want the agent process holding Docker daemon credentials.
- You want gVisor isolation + Squid egress allowlist + XFS quotas
  managed by a dedicated team / image.
- You're operating at a scale where the agent fleet runs in a different
  trust boundary from the sandbox host.

### Trade-offs

- **Persistence ceiling**: per-session volumes are wiped after the
  service's `Limits.hard_destroy_ttl_s` (default 24h of inactivity).
  `DockerBackend` uses the host-mounted per-user dir, which persists
  forever. Operators raise the TTL via
  `ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S` (subject to the upstream
  tenant max), or accept session-bounded persistence and push long-
  lived state to an object store.
- **Multi-tenancy** (since upstream PR #10): each adk-cc tenant maps
  to a distinct service-side tenant with its own scoped token, audit
  log, and Squid allowlist. Operator wires this via the credential
  provider (see "Setup" below). For single-tenant / dev deployments,
  the SHARED_TOKEN env var bypasses the credential provider entirely.
- **No streaming exec**: the service has SSE at `/exec/stream` and
  MCP `progress` notifications via `progressToken` (PR #11), but
  adk-cc's `SandboxBackend.exec` is sync today. The agent waits for
  full stdout/stderr. Background-process logs side-step this — the
  upstream service exposes a process API (PRs #8/#9) but adk-cc has
  not yet surfaced it as a tool surface.
- **Idempotency**: every mutating request adk-cc sends carries an
  `Idempotency-Key` header (upstream PR #7 follow-up). Retries after
  network glitches replay the cached response rather than creating
  duplicate sessions or re-running exec calls.

### Setup

1. Stand up the sandbox service (one of upstream Path A / B / C — see
   their `README.md`). Recommended: Path B (Compose, with published
   images at `ghcr.io/JISUlicious/sandbox-*`).

2. **Single-tenant / dev deployment**: set the shared token:

   ```bash
   ADK_CC_SANDBOX_BACKEND=sandbox_service
   ADK_CC_SANDBOX_SERVICE_URL=https://sandbox.internal:8443
   ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN=<bootstrap bearer>

   # Optional Limits overrides — sent on POST /v1/sessions, subject to
   # the upstream tenant max.
   # ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S=604800   # 7d
   # ADK_CC_SANDBOX_SERVICE_WORKSPACE_GIB=4
   ```

3. **Multi-tenant production deployment**: provision per-tenant
   scoped tokens via the upstream admin API and store them in adk-cc's
   credential provider. For each adk-cc tenant `<tid>`:

   ```bash
   # Create the service-side tenant (admin token required):
   curl -X POST https://sandbox.internal:8443/v1/tenants \
       -H "Authorization: Bearer $SANDBOX_ADMIN_TOKEN" \
       -H "Content-Type: application/json" \
       -d '{"display_name": "<tid>", "limits": {...}}'

   # Issue a scoped token (only the scopes adk-cc actually uses):
   curl -X POST "https://sandbox.internal:8443/v1/tenants/<tid>/tokens" \
       -H "Authorization: Bearer $SANDBOX_ADMIN_TOKEN" \
       -d '{"scopes": ["session_create","session_destroy","exec",
                       "file_read","file_write","file_delete"]}'
   ```

   Store the returned plaintext in adk-cc's credential provider under
   key `sandbox_service_token` (override via
   `ADK_CC_SANDBOX_SERVICE_TOKEN_KEY`). With the existing encrypted-
   file provider:

   ```python
   # Operator script run after issuing the token:
   from adk_cc.credentials import EncryptedFileCredentialProvider
   creds = EncryptedFileCredentialProvider(root="/var/lib/adk-cc/credentials")
   await creds.put(tenant_id="<tid>", key="sandbox_service_token",
                   value="<plaintext-token>")
   ```

   Pass the same provider into `TenancyPlugin`'s `backend_factory` in
   your `make_app` factory so per-session lookup hits it:

   ```python
   from adk_cc.sandbox import make_default_backend

   def _backend(tenant, session_id):
       return make_default_backend(
           session_id=session_id,
           tenant_id=tenant.tenant_id,
           credentials=creds,  # the same provider used for MCP tokens
       )
   ```

   Token rotation: call `POST /v1/tenants/<tid>/tokens` for the new
   token, write it into the credential store, then `DELETE` the old
   token after the 5-min grace window expires. No agent restart
   needed because the backend reads the token at session bring-up.

4. Skill scripts (`run_skill_script`) automatically run inside the
   service via `SandboxBackedCodeExecutor` — no extra wiring.

### Smoke test

```bash
curl -fsSL -H "Authorization: Bearer $TOKEN" \
    https://sandbox.internal:8443/healthz
```

Then drive an agent session; verify a service-side session is created
(`POST /v1/sessions` audit line) and stopped on session end.

### Note: the service's `/mcp` endpoint

The sandbox service also exposes its surface as MCP tools at `/mcp`
for direct LLM consumers (Claude Code/Desktop, Cursor). adk-cc does
NOT use this endpoint — the REST surface is the right shape for a
programmatic Python consumer. If you also want LLM clients to drive
the same sandbox directly, point them at `/mcp` with the same Bearer
token; agent-driven and LLM-driven sessions are isolated by their
own session IDs and don't conflict.


## 7. Alternative: self-hosted Daytona (`daytona` backend)

A second external-service option lives at `ADK_CC_SANDBOX_BACKEND=daytona`.
This delegates to a self-hosted [Daytona](https://daytona.io) compute
plane — open-source, docker-compose or k8s deployable, with native
per-sandbox isolation and per-tenant API keys.

### When to pick this over `sandbox_service`

- You want an open-source compute plane you can host yourself rather
  than depending on the upstream `JISUlicious/sandboxing` service.
- You already operate Daytona for developer workspaces and want adk-cc
  to share the same compute fleet.
- You want native multi-tenant support backed by Daytona's organization
  + API-key model rather than rolling your own credential layer.

### Architecture

Daytona splits operations between two HTTP services, both reachable
from external clients on a stock self-hosted deployment:

1. **Control plane** (NestJS, port 3000): sandbox lifecycle — create,
   start, stop, delete; snapshots; organizations; runners.
2. **Toolbox proxy** (Go, port 4000): per-operation exec / file IO.
   URL-path dispatched (host-header-insensitive), so the agent can
   reach it directly via the host IP regardless of what
   `/api/sandbox/{id}/toolbox-proxy-url` returns.

One adk-cc session maps to one Daytona sandbox. Same Bearer token
authenticates both services.

```
agent process                Daytona deployment
─────────────────            ────────────────────────────
DaytonaBackend
  │
  ├── control plane ────────► <api_url>:3000   (sandbox lifecycle)
  │
  └── toolbox proxy ────────► <proxy_url>:4000 (exec / files)
```

### Trade-offs

- **+** Stronger isolation than `DockerBackend` (per-sandbox kernel,
  filesystem, network stack).
- **+** Open-source; self-hosted via docker-compose or k8s.
- **+** Native multi-tenant: per-organization API keys, quotas,
  audit logs on the Daytona side.
- **+** Sandbox auto-stop + auto-delete reapers handle abandoned
  sessions for free.
- **−** No streaming exec in v1 (the backend inherits the ABC default:
  one chunk at end). v2 will use Daytona's `/process/session` API.
- **−** Stdout and stderr are merged in the exec response (Daytona's
  `{exitCode, result}` shape). v1 surfaces `result` as
  `ExecResult.stdout` with `stderr=""`. Use sessions for split streams.
- **−** Resource sizing (cpu/memory/disk) is dictated by the snapshot
  — Daytona's API rejects request-time resource fields when a snapshot
  is set. Bake the resource profile into your snapshot.

### Setup

1. **Stand up Daytona** via the stock docker-compose distribution.
   Confirm both services are reachable:
   ```bash
   curl http://<daytona-host>:3000/api/health      # control plane
   curl http://<daytona-host>:4000/healthz         # toolbox proxy
   ```

2. **Build an adk-cc snapshot.** The repo's
   `Dockerfile.daytona-snapshot` layers on `daytonaio/sandbox:0.5.0-slim`
   and adds python3, pip, uv, git. From the agent-side repo root:
   ```bash
   docker build -f Dockerfile.daytona-snapshot \
       -t <your-registry>/adk-cc-daytona:latest .
   docker push <your-registry>/adk-cc-daytona:latest
   ```

3. **Register the snapshot in Daytona.** Use the dashboard at
   `http://<daytona-host>:3000/dashboard` or
   `POST /api/snapshots`. Note the snapshot id / name.

4. **Generate an API key.** Dashboard or `POST /api/api-keys`. For
   single-tenant deployments this becomes
   `ADK_CC_DAYTONA_API_KEY`. For multi-tenant, store per-tenant keys
   in your `CredentialProvider` under the `daytona_api_key` key (or
   override the key name via `ADK_CC_DAYTONA_CREDENTIAL_KEY`).

5. **Configure the agent host.** Required env:
   ```bash
   ADK_CC_SANDBOX_BACKEND=daytona
   ADK_CC_DAYTONA_API_URL=http://<daytona-host>:3000
   ADK_CC_DAYTONA_API_KEY=<your bearer token>
   ADK_CC_DAYTONA_SNAPSHOT=adk-cc-daytona      # the name you registered
   ```
   Optional:
   ```bash
   ADK_CC_DAYTONA_PROXY_URL=http://<daytona-host>:4000  # defaults to API host:4000
   ADK_CC_DAYTONA_WORKSPACE_PATH=/home/daytona           # cwd in the sandbox
   ADK_CC_DAYTONA_AUTOSTOP_MIN=15                        # idle pause
   ADK_CC_DAYTONA_AUTODELETE_MIN=1440                    # 24h reaper
   ADK_CC_DAYTONA_DELETE_ON_CLOSE=1                      # ephemeral / CI mode
   ADK_CC_DAYTONA_START_TIMEOUT_S=120                    # cold-start cap
   ```

### Smoke test

After configuring the agent host, drive a one-shot exec via the
adk-cc REPL or any agent session:

```bash
ADK_CC_SANDBOX_BACKEND=daytona \
ADK_CC_DAYTONA_API_URL=http://<host>:3000 \
ADK_CC_DAYTONA_API_KEY=<token> \
python -c "
import asyncio
from adk_cc.sandbox import make_default_backend
from adk_cc.sandbox.config import FsWriteConfig, NetworkConfig
from adk_cc.sandbox.workspace import WorkspaceRoot

async def main():
    b = make_default_backend(session_id='smoke', tenant_id='local')
    ws = WorkspaceRoot(tenant_id='local', session_id='smoke',
                      abs_path='/home/daytona')
    await b.ensure_workspace(ws)
    r = await b.exec('echo hello', fs_write=FsWriteConfig(),
                     network=NetworkConfig(), timeout_s=10, cwd='/home/daytona')
    print('exit:', r.exit_code, '| out:', r.stdout.strip())
    await b.close()
asyncio.run(main())
"
```

You should see `exit: 0 | out: hello` and an audit line on the
Daytona side recording the sandbox create + stop.

### Notes worth knowing (from upstream feedback)

These behaviors of the Daytona API on self-hosted v0.176.0 are
documented as inline comments in
`adk_cc/sandbox/backends/daytona_backend.py` — repeated here for
discoverability:

- **Canonical external route shape**:
  `<proxy>:4000/toolbox/{id}/<route>` (no doubled `/toolbox/` segment).
  The `/api/toolbox/{id}/toolbox/*` routes on the control plane are
  correctly `[DEPRECATED]` — they relay every byte through NestJS and
  don't scale.
- **`proxy.localhost` in `toolbox-proxy-url` is cosmetic.** The Go
  proxy dispatches by URL path, ignoring the Host header for `/toolbox/*`
  routes. We accept the proxy URL via `ADK_CC_DAYTONA_PROXY_URL` rather
  than dereferencing the API. Operators with custom routing can
  override the literal via the (undocumented) `PROXY_TOOLBOX_BASE_URL`
  env on the API container.
- **Snapshot ↔ resources exclusivity**: `POST /api/sandbox` rejects
  `cpu` / `memory` / `disk` when `snapshot` is set
  (`sandbox.controller.ts:304-306` upstream). Our request builder
  elides resource fields whenever a snapshot is in play.
