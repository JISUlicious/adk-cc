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
