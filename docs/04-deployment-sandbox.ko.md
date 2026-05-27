# Sandbox VM 운영자 런북

adk-cc의 `DockerBackend`가 연결할 sandbox 호스트를 프로비저닝하기 위한 1페이지 체크리스트입니다. 시작 전에 end-to-end로 훑어보세요.

## 토폴로지 요약

Agent 프로세스(결국 K8s)가 별도 Linux VM의 Docker 데몬에 TCP로 연결합니다. Agent는 Docker를 로컬로 실행하지 않습니다. 워크스페이스는 sandbox VM의 파일시스템에 상주; agent는 오직 `SandboxBackend` 계약을 통해서만 도달합니다.

```
[agent K8s pod] ──Docker TCP API── [sandbox VM running Docker]
                                            │
                                            ├─ adk-cc-sandbox image
                                            ├─ per-session containers
                                            └─ /var/lib/adk-cc/wks/...
```

## 1. VM 프로비저닝

- **OS**: Ubuntu 22.04 LTS 또는 Rocky Linux 9. 다른 modern Linux distro도 동작; 위는 테스트된 것.
- **하드웨어**: 100 user를 위해 16 physical core, 96 GB RAM, 1 TB NVMe SSD (`02-architecture.ko.md` §5.5 참고).
- **네트워크**: agent의 K8s namespace가 도달 가능한 management subnet에 배치. 다른 모든 inbound 트래픽 차단.
- **단일 목적**: 이 호스트에서 다른 워크로드를 실행하지 마세요. Docker 데몬의 blast radius가 호스트; 호스트를 깨끗하게 유지.

```bash
# Ubuntu — Docker 설치
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 워크스페이스 root
mkdir -p /var/lib/adk-cc/wks
chmod 0755 /var/lib/adk-cc

# sandbox VM에 adk-cc 클론 (Dockerfile만 필요) 후 빌드
git clone https://github.com/JISUlicious/adk-cc.git /opt/adk-cc
cd /opt/adk-cc
docker build -t adk-cc-sandbox:latest -f Dockerfile.sandbox .
```

## 2. 연결 모드 선택

### Plain TCP (더 단순 — 신뢰할 수 있는 내부 네트워크용)

`/etc/docker/daemon.json` 추가:

```json
{
  "hosts": ["unix:///var/run/docker.sock", "tcp://10.0.0.5:2375"]
}
```

`10.0.0.5`을 management 네트워크 IP로 교체.
방화벽 규칙이 cover한다고 확신하지 않는 한 **0.0.0.0 사용 금지**.

```bash
systemctl edit docker
# 추가 (under [Service]):
#   ExecStart=
#   ExecStart=/usr/bin/dockerd
systemctl daemon-reload && systemctl restart docker
```

방화벽(ufw / iptables / cloud security group)을 구성하여 agent의 K8s NAT egress IP만 `tcp://<vm>:2375`에 도달하게 허용.

### TLS TCP (신뢰할 수 없는 hop을 건너는 모든 것에 권장)

CA, 서버 인증서, 클라이언트 인증서를 생성하세요. Docker 문서 <https://docs.docker.com/engine/security/protect-access/>이 canonical reference. 빠른 버전:

```bash
SANDBOX_HOST=sandbox.internal
mkdir -p ~/docker-tls && cd ~/docker-tls

# CA
openssl genrsa -aes256 -out ca-key.pem 4096
openssl req -new -x509 -days 3650 -key ca-key.pem -sha256 -out ca.pem \
  -subj "/CN=adk-cc-ca"

# 서버 인증서
openssl genrsa -out server-key.pem 4096
openssl req -subj "/CN=$SANDBOX_HOST" -sha256 -new \
  -key server-key.pem -out server.csr
echo "subjectAltName = DNS:$SANDBOX_HOST,IP:10.0.0.5" > extfile.cnf
echo "extendedKeyUsage = serverAuth" >> extfile.cnf
openssl x509 -req -days 3650 -sha256 -in server.csr -CA ca.pem \
  -CAkey ca-key.pem -CAcreateserial -out server-cert.pem \
  -extfile extfile.cnf

# 클라이언트 인증서 (agent pod용)
openssl genrsa -out key.pem 4096
openssl req -subj '/CN=adk-cc-agent' -new -key key.pem -out client.csr
echo "extendedKeyUsage = clientAuth" > extfile-client.cnf
openssl x509 -req -days 3650 -sha256 -in client.csr -CA ca.pem \
  -CAkey ca-key.pem -CAcreateserial -out cert.pem \
  -extfile extfile-client.cnf
```

데몬을 mTLS를 요구하도록 구성:

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

`systemctl restart docker`. Agent 호스트에서 확인:

```bash
docker --tlsverify \
  --tlscacert=ca.pem --tlscert=cert.pem --tlskey=key.pem \
  -H tcp://sandbox.internal:2376 \
  version
```

## 3. Agent 구성

Agent의 environment에 설정 (또는 prod용 K8s ConfigMap / Secret):

```bash
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2376
ADK_CC_DOCKER_CA_CERT=/etc/adk-cc/docker-tls/ca.pem
ADK_CC_DOCKER_CLIENT_CERT=/etc/adk-cc/docker-tls/cert.pem
ADK_CC_DOCKER_CLIENT_KEY=/etc/adk-cc/docker-tls/key.pem
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks

# 선택적 spawn-config 튜닝
ADK_CC_SANDBOX_IMAGE=adk-cc-sandbox:latest
ADK_CC_SANDBOX_MEM_LIMIT=4g
ADK_CC_SANDBOX_CPU_QUOTA=100000   # 100k = 1 CPU
ADK_CC_SANDBOX_PIDS_LIMIT=256
```

Plain TCP의 경우: 세 개의 `*_CERT` / `*_KEY` var를 제거하고 `ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2375` 설정.

## 4. Smoke test

Agent 호스트에서 (또는 agent pod 내부에서):

```bash
# 연결성
python -c "
import docker
c = docker.DockerClient(base_url='tcp://sandbox.internal:2376',
    tls=docker.tls.TLSConfig(client_cert=('cert.pem','key.pem'),
                             ca_cert='ca.pem', verify=True))
print(c.version())
"
```

그 다음 `adk api_server`를 sandbox에 대해 구동; per-session container가 나타나고(`docker ps`) 세션 종료 후 사라지는지 확인(`docker.close()`이 `after_run_callback`에서 실행).

## 5. 운영 고려사항

- **이미지 업데이트**: 새 adk-cc 커밋 풀 후 sandbox VM에서 `adk-cc-sandbox:latest` 재빌드. 재빌드 전 시작된 세션은 캐시 레이어 계속 사용; 새 세션은 업데이트 받음.
- **워크스페이스 백업**: `/var/lib/adk-cc/wks`는 per-tenant 데이터. 보존 SLA에 맞는 스케줄로 볼륨 스냅샷.
- **컨테이너 leak**: agent pod이 세션 도중 충돌하면 per-session container가 orphan될 수 있음. 주기적으로 실행:
  ```bash
  docker ps --filter label=adk-cc-session --format '{{.Names}} {{.Status}}'
  # Agent reachability 없이 >24h Up인 것 reap
  ```
- **로깅**: 컨테이너는 기본적으로 stdout/stderr가 forward되지 않음 (모델은 API를 통해 exec 결과 받음). 디버깅을 위해 attach: `docker logs adk-cc-<session_id>`.
- **자원 ceiling**: 컨테이너별 limit이 spawn에서 설정됨. 티어를 올리려면 `ADK_CC_SANDBOX_MEM_LIMIT=8g` (또는 더 높게) 설정 후 agent 재시작 — 새 세션이 새 limit 받음.
- **디스크 압박**: Docker overlay가 커질 수 있음. cron에서 `docker system df`와 `docker system prune --volumes` 실행.

## 6. 대안: 외부 sandbox service (`sandbox_service` 백엔드)

Sandbox 책임을 agent 프로세스에서 완전히 분리하고 싶은 배포 — 일반적으로 managed multi-tenant SaaS — 를 위해 adk-cc는 외부 REST sandbox service와 대화하는 `SandboxServiceBackend`를 출하합니다. 오늘의 reference 구현:
[JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing).

### `DockerBackend` 대신 선택할 때

- Agent 프로세스가 Docker 데몬 자격 증명을 가지길 원하지 않음.
- 전담 팀 / 이미지가 관리하는 gVisor 격리 + Squid egress allowlist + XFS quota를 원함.
- Agent fleet이 sandbox 호스트와 다른 trust 경계에서 운영되는 규모.

### Trade-off

- **영속화 ceiling**: per-session 볼륨은 service의 `Limits.hard_destroy_ttl_s` (기본 비활성 24h) 후 와이프됨. `DockerBackend`는 호스트 마운트 per-user dir 사용, 영원히 persist. 운영자는 `ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S` (upstream 테넌트 최대치 적용)로 TTL을 올리거나, 세션 한정 persistence를 수용하고 long-lived 상태를 object store에 push.
- **다중 테넌트** (upstream PR #10 이후): 각 adk-cc 테넌트가 자체 scoped token, audit log, Squid allowlist를 가진 별도의 service-side 테넌트에 매핑. 운영자가 credential provider를 통해 와이어링 ("Setup" 아래 참고). 단일 테넌트 / dev 배포의 경우 SHARED_TOKEN env var이 credential provider를 완전히 우회.
- **Streaming exec 없음**: service는 `/exec/stream`에 SSE와 `progressToken` (PR #11)을 통한 MCP `progress` 알림을 갖지만, adk-cc의 `SandboxBackend.exec`은 오늘 동기. Agent는 전체 stdout/stderr를 기다림. 백그라운드 프로세스 로그가 이를 side-step — upstream service는 process API (PR #8/#9)를 노출하지만 adk-cc는 아직 tool surface로 surface하지 않음.
- **Idempotency**: adk-cc가 보내는 모든 mutating 요청이 `Idempotency-Key` 헤더 운반 (upstream PR #7 후속). 네트워크 결함 후 재시도는 중복 세션 생성이나 exec 호출 재실행 대신 캐시된 응답 재생.

### 셋업

1. Sandbox service를 stand up하세요 (upstream Path A / B / C 중 하나 — 그들의 `README.md` 참고). 권장: Path B (Compose, `ghcr.io/JISUlicious/sandbox-*`에 published image와 함께).

2. **단일 테넌트 / dev 배포**: shared 토큰 설정:

   ```bash
   ADK_CC_SANDBOX_BACKEND=sandbox_service
   ADK_CC_SANDBOX_SERVICE_URL=https://sandbox.internal:8443
   ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN=<bootstrap bearer>

   # 선택적 Limits override — POST /v1/sessions에 전송, upstream 테넌트 최대치 적용.
   # ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S=604800   # 7d
   # ADK_CC_SANDBOX_SERVICE_WORKSPACE_GIB=4
   ```

3. **다중 테넌트 프로덕션 배포**: upstream admin API를 통해 per-tenant scoped 토큰을 프로비저닝하고 adk-cc의 credential provider에 저장. 각 adk-cc 테넌트 `<tid>`에 대해:

   ```bash
   # Service-side 테넌트 생성 (admin token 필요):
   curl -X POST https://sandbox.internal:8443/v1/tenants \
       -H "Authorization: Bearer $SANDBOX_ADMIN_TOKEN" \
       -H "Content-Type: application/json" \
       -d '{"display_name": "<tid>", "limits": {...}}'

   # Scoped 토큰 발급 (adk-cc가 실제 사용하는 scope만):
   curl -X POST "https://sandbox.internal:8443/v1/tenants/<tid>/tokens" \
       -H "Authorization: Bearer $SANDBOX_ADMIN_TOKEN" \
       -d '{"scopes": ["session_create","session_destroy","exec",
                       "file_read","file_write","file_delete"]}'
   ```

   반환된 plaintext를 adk-cc의 credential provider에 `sandbox_service_token` 키 (오버라이드 via `ADK_CC_SANDBOX_SERVICE_TOKEN_KEY`)로 저장. 기존 encrypted-file provider와 함께:

   ```python
   # 토큰 발급 후 실행되는 운영자 스크립트:
   from adk_cc.credentials import EncryptedFileCredentialProvider
   creds = EncryptedFileCredentialProvider(root="/var/lib/adk-cc/credentials")
   await creds.put(tenant_id="<tid>", key="sandbox_service_token",
                   value="<plaintext-token>")
   ```

   같은 provider를 `make_app` 팩토리의 `TenancyPlugin`의 `backend_factory`에 전달하여 per-session lookup이 hit하게 함:

   ```python
   from adk_cc.sandbox import make_default_backend

   def _backend(tenant, session_id):
       return make_default_backend(
           session_id=session_id,
           tenant_id=tenant.tenant_id,
           credentials=creds,  # MCP 토큰에 사용된 같은 provider
       )
   ```

   토큰 로테이션: 새 토큰을 위해 `POST /v1/tenants/<tid>/tokens` 호출, credential store에 쓰기, 5분 grace window 만료 후 옛 토큰 `DELETE`. 백엔드가 세션 bring-up에서 토큰을 읽으므로 agent 재시작 불필요.

4. Skill 스크립트(`run_skill_script`)는 `SandboxBackedCodeExecutor`를 통해 service 안에서 자동 실행 — 추가 와이어링 없음.

### Smoke test

```bash
curl -fsSL -H "Authorization: Bearer $TOKEN" \
    https://sandbox.internal:8443/healthz
```

그 다음 agent 세션 구동; service-side 세션이 생성되고(`POST /v1/sessions` audit 라인) 세션 종료 시 중지되는지 확인.

### 노트: service의 `/mcp` endpoint

Sandbox service는 직접 LLM 소비자(Claude Code/Desktop, Cursor)용 MCP 도구로 자신의 surface를 `/mcp`에도 노출합니다. adk-cc는 이 endpoint를 사용하지 않음 — REST surface가 프로그래매틱 Python 소비자에 맞는 모양. LLM 클라이언트도 같은 sandbox를 직접 구동하고 싶다면 같은 Bearer 토큰으로 `/mcp`를 가리키세요; agent 주도와 LLM 주도 세션은 자체 세션 ID로 격리되어 충돌하지 않습니다.
