# 프로덕션 배포

이것은 adk-cc를 노트북의 `adk web .`에서 FastAPI 서비스로 가져가기 위한 런북 + readiness checklist입니다. 프로덕션을 stand up하기 전에 end-to-end로 읽으세요; 순서가 중요합니다.

> **Status: alpha.** adk-cc는 기능적이며 end-to-end로 검증되었지만(~135 unit + e2e 체크, 11 테스트 파일) 실제 배포의 운영 충격에 아직 hardened되지 않았습니다. 아래 checklist는 동작하는 것(✓), 부분적인 것(⚠️), 누락된 것(✗)을 정직하게 표시합니다. 운영자는 실제 사용자에게 서비스하기 전에 위협 모델과 SLO에 맞는 ✗ 항목을 닫아야 합니다.

## 같은 팩토리에서 두 가지 배포 모양

`make_app` 팩토리는 둘 다 서빙:

- **단일 인스턴스, 단일 테넌트.** 서버 하나, 팀 하나, 정적 토큰 인증, sqlite 세션 저장소, 로컬 Docker 또는 co-located sandbox service. 가장 작은 viable 프로덕션. README의 "최소 단일 테넌트 프로덕션 레시피" 절이 이를 end-to-end로 다룹니다.
- **다중 테넌트 SaaS.** 테넌트 클레임이 있는 JWT 인증, postgres 세션 저장소, per-tenant 자격 증명 / MCP / skill 레지스트리. 아래의 전체 토폴로지.

둘 사이의 차이는 **어떤 env 변수 + per-tenant 자원을 와이어링하는가**이지, 어떤 팩토리나 어떤 코드 경로가 아닙니다. 단일 테넌트 플레이버는 단순히 하나의 테넌트만 있는 다중 테넌트; per-tenant 자원 레이아웃은 여전히 적용 (`<wks>/<tenant>/<user>/` 아래 워크스페이스 등). 이 문서는 둘 다 하나의 배포 스토리로 다룹니다; 모양과 무관하게 적용되는 갭 목록은 readiness checklist로 점프하세요.

## 토폴로지

```
                                                       ┌──────────────┐
            ┌────────────────────────────┐             │   IdP        │
            │  K8s cluster               │             │  (issues     │
            │                            │ JWKS fetch  │   JWTs)      │
            │   ┌────────────────────┐   │◄──HTTPS─────┤              │
            │   │  adk-cc agent pod  │   │             └──────────────┘
            │   │  - JwtAuthMW       │   │
            │   │  - FastAPI factory │   │ Docker mTLS ┌──────────────┐
            │   │  - DockerBackend ──┼───┼─port 2376───►  Sandbox VM  │
            │   └────┬───────────────┘   │             │  (per-       │
            │        │                   │             │   session    │
            └────────┼───────────────────┘             │   containers)│
                     │ TCP 5432                        └──────────────┘
                     ▼
            ┌─────────────────────┐
            │  Postgres           │
            │  (ADK sessions)     │
            └─────────────────────┘
```

Agent pod이 의존하는 다섯 가지 외부 의존성:

1. **IdP** — agent가 수용할 JWT 발급. 안정적인 URL에 JWKS 제공.
2. **Postgres** — ADK 세션 저장소용. 수백 user에 단일 인스턴스로 충분; ADK의 세션 스키마에 맞춰 사이징.
3. **Sandbox VM** — Docker 데몬 실행, agent pod에서만 mTLS 연결 수용. [`04-deployment-sandbox.ko.md`](./04-deployment-sandbox.ko.md) 참고.
4. **Persistent volume** — agent pod의 task / 자격 증명 / 테넌트 레지스트리 / audit 로그용. 워크스페이스는 sandbox VM에 상주, 여기 아님.
5. **모델 서버** (LLM) — agent가 `LiteLlm`을 통해 대화. Hosted Anthropic / OpenAI endpoint이거나 자체 호스트 vLLM / mlx_lm.

## Step-by-step 배포

### 1. Sandbox VM (일회성)

[`04-deployment-sandbox.ko.md`](./04-deployment-sandbox.ko.md)을 따라 Linux VM 프로비저닝, Docker 설치, `adk-cc-sandbox:latest` 빌드, mTLS 구성, 인증서 쌍 생성. VM의 호스트명과 `/var/lib/adk-cc/wks`에 선택한 경로를 기록.

### 2. Postgres

```sql
CREATE DATABASE adk_cc;
CREATE USER adk_cc WITH PASSWORD '<pick>';
GRANT ALL PRIVILEGES ON DATABASE adk_cc TO adk_cc;
```

ADK가 첫 사용에서 세션 스키마를 생성. DSN은 `ADK_CC_SESSION_DSN`에 들어감.

### 3. Fernet 자격 증명 키 생성 (일회성)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Secret manager에 저장. 손실 = 등록된 자격 증명 복호화 불가. 노출 = 모든 테넌트의 자격 증명 전체 공개.

### 4. JWT 검증 와이어링

세 개의 필수 env 변수:

```
ADK_CC_JWT_JWKS_URL=https://idp.example.com/.well-known/jwks.json
ADK_CC_JWT_ISSUER=https://idp.example.com
ADK_CC_JWT_AUDIENCE=adk-cc
```

선택 (기본값 표시):

```
ADK_CC_JWT_USER_CLAIM=sub        # user id인 JWT 클레임
ADK_CC_JWT_TENANT_CLAIM=tenant   # tenant id인 JWT 클레임
```

IdP는 클라이언트에 발급하는 토큰에 두 클레임 모두 포함해야 합니다. IdP가 다른 클레임 이름을 사용하면 위에서 오버라이드. IdP가 tenant 클레임을 포함하지 않으면 **아직 배포하지 마세요** — 커스텀 `AuthExtractor` 구현 ("Custom auth" 아래 참고).

### 5. 전체 env config

[`../.env.example`](../.env.example)에서 시작. 최소 프로덕션 세트:

```bash
# 모델
ADK_CC_API_KEY=...
ADK_CC_API_BASE=https://your-llm-host/v1
ADK_CC_MODEL=...

# 서비스
ADK_CC_AGENTS_DIR=/srv/adk-cc           # adk_cc/의 상위
ADK_CC_SESSION_DSN=postgresql://adk_cc:...@postgres:5432/adk_cc
ADK_CC_PERMISSION_MODE=default

# 인증 (프로덕션)
ADK_CC_JWT_JWKS_URL=...
ADK_CC_JWT_ISSUER=...
ADK_CC_JWT_AUDIENCE=adk-cc

# Sandbox
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=tcp://sandbox.internal:2376
ADK_CC_DOCKER_CA_CERT=/etc/adk-cc/docker-tls/ca.pem
ADK_CC_DOCKER_CLIENT_CERT=/etc/adk-cc/docker-tls/cert.pem
ADK_CC_DOCKER_CLIENT_KEY=/etc/adk-cc/docker-tls/key.pem
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks   # SANDBOX VM의 경로

# Per-tenant 자원 (다중 테넌트 SaaS)
ADK_CC_TENANT_REGISTRY_DIR=/var/lib/adk-cc/tenants
ADK_CC_CREDENTIAL_PROVIDER=encrypted_file
ADK_CC_CREDENTIAL_KEY=<paste-fernet-key>
ADK_CC_CREDENTIAL_STORE_DIR=/var/lib/adk-cc/credentials
ADK_CC_TENANT_SKILLS_DIR=/var/lib/adk-cc/skills

# Tasks / audit
ADK_CC_TASKS_DIR=/var/lib/adk-cc/tasks
ADK_CC_AUDIT_LOG=/var/log/adk-cc/audit.jsonl

# Context 가드레일 (프로덕션 권장)
ADK_CC_MAX_CONTEXT_TOKENS=100000          # 메인 모델 window
ADK_CC_COMPACTION_TOKEN_THRESHOLD=70000   # ADK가 이 이상에서 compact
ADK_CC_COMPACTION_EVENT_RETENTION=10      # 마지막 N개 raw event 유지
# ADK_CC_COMPACTION_MODEL=openai/gpt-4o-mini   # 선택적 더 저렴한 compaction 모델
```

`/var/lib/adk-cc/{tasks,credentials,tenants,skills}`와 `/var/log/adk-cc/`을 persistent volume에서 마운트; pod 재시작 시 상태 손실.

### 6. 실행

```bash
uvicorn adk_cc.service.server:make_app --factory \
  --host 0.0.0.0 --port 8000 --workers 4
```

`make_app`은 `ADK_CC_JWT_JWKS_URL`과 `ADK_CC_AUTH_TOKENS` 모두 unset이면 fail-closed (`ADK_CC_ALLOW_NO_AUTH=1`이 아닌 한). 프로덕션은 JWT에 고정.

### 7. (선택) Tenant self-serve용 admin route 마운트

`make_app`은 기본적으로 admin route를 마운트하지 않습니다. 테넌트가 HTTP로 자격 증명/MCP/skill 등록을 self-serve할 거면 thin wrapper 작성:

```python
# my_factory.py
import os
from adk_cc.service.server import build_fastapi_app
from adk_cc.service.admin_routes import mount_tenant_admin
from adk_cc.credentials import EncryptedFileCredentialProvider
from adk_cc.service.registry import JsonFileTenantResourceRegistry
from adk_cc.tools.mcp_tenant import McpServerConfig
from adk_cc.service.auth import JwtAuthExtractor

def app():
    extractor = JwtAuthExtractor(
        jwks_url=os.environ["ADK_CC_JWT_JWKS_URL"],
        issuer=os.environ["ADK_CC_JWT_ISSUER"],
        audience=os.environ["ADK_CC_JWT_AUDIENCE"],
    )
    creds = EncryptedFileCredentialProvider(
        root=os.environ["ADK_CC_CREDENTIAL_STORE_DIR"],
    )
    registry = JsonFileTenantResourceRegistry[McpServerConfig](
        root=os.environ["ADK_CC_TENANT_REGISTRY_DIR"],
        kind="mcp", model=McpServerConfig, id_attr="server_name",
    )
    fastapi_app = build_fastapi_app(
        agents_dir=os.environ["ADK_CC_AGENTS_DIR"],
        session_service_uri=os.environ.get("ADK_CC_SESSION_DSN"),
        auth_extractor=extractor,
        # ... 기타 build_fastapi_app 인자
    )
    mount_tenant_admin(
        fastapi_app, registry=registry, credentials=creds,
        skill_root=os.environ.get("ADK_CC_TENANT_SKILLS_DIR"),
    )
    return fastapi_app
```

그 다음 `uvicorn my_factory:app --factory ...`. `mount_tenant_admin`의 기본 RBAC은 "caller의 tenant가 target tenant와 같아야 함"; global-admin 패턴은 `admin_extractor=`로 전달.

### 8. Smoke test

```bash
# 미인증 → 401
curl -i https://your-host/tenants/tenantA/mcp-servers
# 예상: HTTP/1.1 401

# Valid JWT → 200
curl -i https://your-host/tenants/tenantA/mcp-servers \
  -H "Authorization: Bearer $JWT"
# 예상: 200 with empty servers list
```

라이브 agent를 통해 세션 구동 (placeholder 교체):

```bash
SESSION=test-$(date +%s)
curl -X POST https://your-host/apps/adk_cc/users/alice/sessions/$SESSION \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" -d '{}'

curl -X POST https://your-host/run \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d "{\"appName\":\"adk_cc\",\"userId\":\"alice\",\"sessionId\":\"$SESSION\",
       \"newMessage\":{\"role\":\"user\",\"parts\":[{\"text\":\"echo hello\"}]}}"
```

Sandbox VM에서 `docker ps --filter label=adk-cc-session`이 per-session container를 보여주는지 확인; 세션 종료 후 사라지는지 확인.

## Custom auth

`make_app`은 두 스톡 extractor (JWT, dev BearerToken)을 출하. 그 외 무엇이든(mTLS 클라이언트 인증서, 세션 DB, OAuth introspection) `AuthExtractor` 프로토콜 구현:

```python
class MyAuthExtractor:
    async def __call__(self, request) -> tuple[str, str]:
        # ... 로직, (user_id, tenant_id) 반환 또는 HTTPException 발생
```

그 다음 `build_fastapi_app(auth_extractor=...)`로 직접 앱을 구성하고 `make_app`을 완전히 건너뜀.

## 워크스페이스 저장소 티어

프로덕션의 워크스페이스는 `ADK_CC_WORKSPACE_ROOT` 아래 `<tenant>/<user>/`로 스코프됩니다. 사용자 수 기반 세 가지 운영 티어; 배포 시 하나 선택. 경로 레이아웃(`<root>/<tenant>/<user>/`)은 세 티어 모두 동일 — Tier 2 / 3는 저장소가 물리적으로 어디 사는지만 다름.

### Tier 1 — 단일 sandbox VM (≤ ~500 user)

Sandbox VM의 로컬 NVMe. `<root>`는 VM의 디렉터리 (예: `/var/lib/adk-cc/wks`). Per-session container가 `<user_home>`을 로컬 디스크에서 직접 bind-mount.

- **디스크 사이징**: user당 ~5 GB × 500 = 2.5 TB. 하나의 NVMe에 들어감.
- **백업**: ZFS / btrfs / LVM 스냅샷을 off-host로 (`zfs send`, S3 호환에 `restic`). User dir이 자연스러운 단위.
- **동시성 cap**: ~10 동시 세션 × 4 GB = 40 GB peak. 10 이후, quota 플러그인(`ADK_CC_QUOTA_PER_MINUTE`)이 친절한 메시지로 거부.
- **GDPR 삭제**: `rm -rf <root>/<tenant>/<user>/`.
- **테넌트 offboarding**: `rm -rf <root>/<tenant>/`.

### Tier 2 — 공유 FS의 multi-VM (~500–5K user) — 경로 호환, 미구현

모든 sandbox VM에서 `<root>`에 마운트된 NFS / EFS / Azure Files. 같은 `<root>/<tenant>/<user>/` 경로. Per-tenant 디스크 cap을 위한 FS 레벨 quota (NFS quota).

- **Tradeoff**: NFS I/O는 로컬 NVMe보다 3–10× 느림. 코드 편집 + plan 파일에는 괜찮; 큰 데이터 ingest에는 고통. Per-user `.cache/`을 로컬 NVMe에 유지하여 mitigate (네트워크 FS에서 lazy 재빌드).
- **실패 모드**: NFS 서버는 단일 실패 포인트. 복제와 함께 managed service (EFS / Filestore) 사용.

### Tier 3 — VM-affinity sharded (≥ ~5K user) — 경로 호환, 미구현

각 sandbox VM이 `user_id` consistent hash로 user 슬라이스 소유. 워크스페이스는 할당된 VM의 로컬 NVMe에 상주. Cross-VM 액세스는 control-plane 작업 (drain → rsync → 라우팅 엔트리 swap). v1 scope 밖.

### Tier 4 — service-mediated (any size, sandbox 분리)

`ADK_CC_SANDBOX_BACKEND=sandbox_service`이 exec / file IO를 외부 sandbox service ([JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing) 또는 호환)에 위임. 워크스페이스는 service의 호스트 볼륨 (`SANDBOX_VOLUME_BASE`, 기본 `/var/lib/sandbox-volumes`)에 상주, agent 호스트가 아님.

- **영속화 ceiling**: `Limits.hard_destroy_ttl_s` (기본 86400 — 비활성 24h)가 적용. Tier 1/2의 무기한 per-user persistence 약속은 여기서 **유지되지 않음**; `ADK_CC_SANDBOX_SERVICE_HARD_DESTROY_TTL_S` (upstream 테넌트 최대치 적용) 올리거나 long-lived 상태를 외부 object store에 push.
- **Wire 상의 다중 테넌트**: upstream PR #10 이후, 각 adk-cc 테넌트가 자체 scoped 토큰, audit 로그, Squid egress allowlist를 가진 별도의 service-side 테넌트에 매핑. 운영자가 admin API를 통해 테넌트 + 토큰 프로비저닝 후 adk-cc의 credential provider에 토큰 저장; 백엔드가 per-session resolve. 단일 테넌트 배포는 여전히 `ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN`을 dev/단일 테넌트 탈출구로 사용 가능.
- **Idempotency**: adk-cc의 모든 mutating 요청이 `Idempotency-Key` 헤더 운반 (upstream PR #7 후속); 일시적 재시도가 중복 세션 생성이나 명령 재실행 안 함.
- **백업/복원**: service의 `SANDBOX_VOLUME_BASE` 스냅샷 (운영자 제어). Service 내부 per-user dir 명명은 adk-cc의 `WorkspaceRoot` 모양에 의존; audit 로그의 session ↔ user 매핑을 통해 per-adk-cc-user 백업에 매핑.
- **운영자 셋업**: `04-deployment-sandbox.ko.md` §6와 upstream README의 세 설치 경로 (A: 로컬 dev, B: Compose, C: systemd) 참고.

### Per-user 설치 캐시

`DockerBackend`이 `<user_home>/.cache`를 per-session container 내부의 `/root/.cache`에 bind-mount. uv/pip 캐시가 user의 세션 간 살아남으므로 cold-install 지연은 첫 세션에만.

진정한 stateless 컨테이너(모든 세션이 모든 것 재설치)를 위해 `ADK_CC_DISABLE_INSTALL_CACHE_MOUNT=1`로 비활성.

### 세션 scratch 보유

`<user_home>/.sessions/<session>/`의 per-session scratch dir이 축적됩니다. `scripts/scratch_reaper.py`이 `ADK_CC_SESSION_SCRATCH_RETENTION_DAYS` (기본 7)보다 오래된 dir을 reap. cron / systemd 타이머로 와이어:

```bash
# /etc/cron.daily/adk-cc-scratch-reaper
0 3 * * * /usr/bin/python3 /opt/adk-cc/scripts/scratch_reaper.py \
  --root /var/lib/adk-cc/wks --max-age-days 7
```

User home dir은 절대 reap되지 않음 — user의 persistent 상태.

### 지식 consolidation (memory + wiki)

자율 memory(episodic→semantic)와 공유 wiki(inbox→domain) 모두 out-of-band로 consolidate합니다. consolidation은 공유 per-tenant/per-user 파일을 **mutate**하며, 상호 배제는 `threading.Lock` — *한 프로세스 내에서만* 동작합니다. 따라서 규칙은: **동시에 consolidator는 정확히 하나.**

**단일 프로세스 (replica 1, worker 1 — 개발/소규모):** in-process로 실행. Memory: `ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S` 설정(스케줄러가 API 서버 lifespan에서 consolidation + compaction 실행) + 선택적으로 `ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD`(capture-path에서 즉시 promote). wiki는 여전히 librarian cron 필요. 외부 memory cron은 불필요.

**Multi-worker (`uvicorn --workers N`) 또는 k8s (`replicas > 1`):** lock이 프로세스/pod를 넘지 못하므로, N개의 in-process 스케줄러가 작업을 중복하고 같은 파일에서 race. 대신 serving pod를 **capture-only**로 두고 단일 외부 consolidator를 실행:

- Serving: `ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S`와 `ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD`를 **unset**(capture는 append-only / unique-id라 pod 간 안전). `ADK_CC_MEMORY=1` / `ADK_CC_WIKI=1`은 유지.
- consolidator 하나(cron / systemd 타이머 / k8s **CronJob**)가 두 pass 실행:

```bash
# 매시간 — consolidate된 memory + domain wiki의 유일한 writer
0 * * * * cd /opt/adk-cc && .venv/bin/python scripts/memory_consolidator.py \
            --root /var/lib/adk-cc/.memory --model --compact
5 * * * * cd /opt/adk-cc && .venv/bin/python scripts/wiki_librarian.py \
            --root /var/lib/adk-cc/.wiki --compact
```

k8s에서는 같은 이미지로 위 명령을 실행하는 두 개의 `CronJob`. **전제조건:** 모든 replica *와* CronJob이 `ADK_CC_MEMORY_STORE_URI` / `ADK_CC_WIKI_STORE_URI`(스토리지 서비스 — pod-local 디스크는 pod마다 별도 지식을 가짐)로 저장소를 공유해야 하고, 해당 backend가 changelog/인덱스가 쓰는 docstore `append` / `kv_*` 연산을 구현해야 함(filesystem backend는 구현됨; S3/db backend는 추가 필요). CronJob의 대안 — leader election(k8s `Lease`)이나 distributed lock(Redis/DB) — 은 더 무거움; CronJob이 기본.

`--model`은 LLM synthesis/resolution/verification을 켬; deterministic·모델 없는 pass는 생략. `--compact`은 중복 재병합(memory Fix F / wiki domain compaction)을 추가.

### Pre-per-user 레이아웃에서 migration

기존 tenant-shared 배포 (`<root>/<tenant>/...`이며 파일이 테넌트 root에, `<user>/` 중첩 없음)는 테넌트당 일회성 migration이 필요합니다. 두 경우:

1. **One-user-per-tenant**: `mv <root>/<tenant>/* <root>/<tenant>/<user>/`. 사소.
2. **Tenant-shared 파일**: 전략 선택 — "shared" user home으로 복사, 모든 user의 home으로 복사, 또는 archive 후 fresh 시작. 운영자 결정.

Migration 전 스냅샷; rollback = 스냅샷에서 복원.

Tasks dir: `mv ~/.adk-cc/tasks/<tenant>/<session>/*.json <root>/<tenant>/<user>/.adk-cc/tasks/<session>/`. Session→user 매핑 필요 (user_id에서 join한 Postgres `sessions` 테이블).

## 프로덕션 readiness checklist

실제 사용자에게 서비스하기 전에 각각 표시. ✓ = adk-cc가 cover; ⚠️ = 부분적 / caveat 있음; ✗ = 운영자가 추가해야.

### 보안

- ✓ **Fail-closed 인증.** `make_app`은 `ADK_CC_ALLOW_NO_AUTH=1`이 설정되지 않은 한 extractor 없이 시작 거부.
- ✓ **JWT 검증.** JWKS에 대한 서명 (TTL 캐시), exp/nbf, iss, aud, 설정 가능한 user/tenant 클레임.
- ✓ **Sandbox 격리.** DockerBackend per-session container: read-only rootfs, `cap_drop=ALL`, `no-new-privileges`, 기본 `network_mode=none`, mem/cpu/pids 제한, unprivileged user.
- ✓ **자격 증명 휴면 시 암호화.** Fernet, env / secret manager의 키.
- ⚠️ **Agent → Docker 데몬 신뢰.** Agent pod이 sandbox VM의 전체 Docker 데몬 API를 가짐. mTLS + 네트워크 ACL로 한정되지만 여전히 광범위. `SandboxBackend` 계약만 노출하는 thin RPC 서비스로 좁히는 것은 Stage-2 follow-up.
- ⚠️ **Permissions YAML.** `ADK_CC_PERMISSIONS_YAML` 스키마가 `adk_cc/config/settings_loader.py`에 문서화되어 있지만 loader가 시작 시 lint하지 않음; 잘못된 규칙은 첫 거부된 호출에서 surface.
- ✗ **HTTP rate limiting.** Per-tenant tool-call rate cap 존재 (`ADK_CC_QUOTA_PER_MINUTE`). HTTP 레벨 rate limit 없음 (로그인 throttling, per-IP). ingress (nginx, Envoy, ALB)나 middleware로 추가.
- ✗ **Audit 로그 무결성.** `AuditPlugin`이 append-only JSONL 작성. Tamper-evidence (서명된 receipt, hash chain, 외부 sink) 없음. 규제 워크로드는 JSONL을 immutable store (S3 Object Lock, append-only Splunk/Elastic)에 출하.
- ✗ **의존성 CVE 스캐닝.** 아직 CI 없음; `uv pip install` + `pip-audit` 또는 `trivy`를 pre-deploy 체크에 와이어.
- ✗ **Secret 로테이션.** `ADK_CC_CREDENTIAL_KEY` 로테이션 (모든 저장된 자격 증명 재암호화 필요), JWKS 키 (rolling), Postgres 비밀번호에 대한 문서화된 절차 없음. 출시 전 계획.

### 신뢰성

- ✓ **다중 worker 안전.** `JsonFileTaskStorage`, `EncryptedFileCredentialProvider`, `JsonFileTenantResourceRegistry` 모두 `filelock` 사용하여 다중 uvicorn worker가 경쟁하지 않음.
- ⚠️ **단일 sandbox VM.** 16-core / 96 GB 호스트에서 ~10 동시 세션 × 4 GB로 capacity 계획 (~500 user에 적합; `02-architecture.ko.md` §5.5 참고). 그 이후, `session_id` consistent hashing을 통한 multi-VM 스케일링은 문서화되어 있지만 미구현.
- ✗ **Container leak reaper.** Agent pod이 세션 도중 충돌하면 per-session container가 orphan될 수 있음. `04-deployment-sandbox.ko.md`의 런북이 수동 reap을 문서화; 프로덕션은 `docker ps --filter label=adk-cc-session --format ...`을 실행하고 agent reachability 없이 Up >1h인 container를 reap하는 cron / systemd 타이머 추가.
- ✗ **Idle-timeout 워치독.** `DockerBackend`는 `after_run_callback`에서만 정리. 긴 일시정지를 생성하는 모델이 container를 hot으로 유지. 비용이 중요하면 워치독 추가.
- ✗ **세션 타임아웃.** ADK 세션은 기본적으로 만료되지 않음. 하드 cap을 원하는 운영자는 세션 서비스 또는 janitor로 와이어.
- ✗ **LLM 재시도 / circuit-breaker.** LiteLLM은 내부 재시도가 있음; 그 위의 일시 에러에 대한 surface된 정책 없음. Flaky 모델 서버가 user 대면 500을 spike 가능.
- ✓ **Context-length 가드레일.** ADK의 `EventsCompactionConfig`이 `LlmEventSummarizer`를 통해 invocation 후 token-threshold compaction 실행 (set `ADK_CC_COMPACTION_TOKEN_THRESHOLD` + `ADK_CC_COMPACTION_EVENT_RETENTION`; `ADK_CC_COMPACTION_MODEL`을 통한 선택적 전용 compaction 모델). adk-cc는 ADK가 compact하기 전에 한 step에서 window를 넘어 점프하는 드문 turn을 잡기 위해 pre-flight WARN 로깅과 fail-soft REJECT (`ADK_CC_MAX_CONTEXT_TOKENS`, `ADK_CC_CONTEXT_WARN_TOKENS`, `ADK_CC_CONTEXT_REJECT_TOKENS`)를 위한 `ContextGuardPlugin` 추가. `02-architecture.ko.md` §7.5 참고.

### 관측성

- ✓ **Tool-call audit.** `AuditPlugin`이 tool 시도당 하나의 JSONL 라인 작성 — 거부 포함.
- ⚠️ **Tracing.** 프로세스 시작 시 tracer가 구성되면 ADK가 OpenTelemetry span emit. `OTEL_EXPORTER_OTLP_ENDPOINT`을 와이어하고 팩토리에 `OpenTelemetryInstrumentor` 추가; 그렇지 않으면 trace가 drop됨.
- ✗ **`/healthz`.** Liveness / readiness endpoint 없음. 팩토리에 추가: Postgres reachable이면 200 반환하는 `@app.get("/healthz")`.
- ✗ **Prometheus 메트릭.** 오늘 노출된 것 없음. 추가할 유용한 시리즈: route별 request latency p50/p95/p99; tool별 tool-call count + error rate; sandbox container count; quota 거부 count; auth 실패 count.
- ✗ **구조화 로그.** 기본 로깅은 비구조화 Python `logging`. JSON formatter (예: `python-json-logger`) 와이어하여 로그 aggregator가 필드 인덱스.
- ✗ **SLI / SLO.** 출시 전 정의: "p95 tool-call latency under X", "세션 생성 성공률 above Y", "auth 실패율 below Z".

### 운영

- ✗ **Agent 프로세스 Dockerfile.** `Dockerfile.sandbox`만 출하됨 (sandbox VM의 per-session container용). Agent pod 자체는 자체 Dockerfile 필요 — 단순함 (`FROM python:3.12-slim`, `uv pip install -e .`, uvicorn entrypoint)지만 직접 작성해야. 배포 repo의 K8s manifest와 함께 체크인.
- ✗ **K8s manifest / Helm chart.** 제공되지 않음. 최소: Deployment, Service, ConfigMap (env), Secret (creds + JWT keys + Docker mTLS 인증서), PersistentVolumeClaim, NetworkPolicy (IdP egress + Postgres + sandbox VM Docker 포트만 허용).
- ✗ **Graceful 종료.** `uvicorn`이 SIGTERM 처리, 하지만 ADK가 문서화된 세션 flush 후크가 없음. In-flight `run_bash` 호출은 pod 종료 시 사망. Stateless tool에는 수용 가능; long-running에는 먼저 로드 밸런서로 drain.
- ✗ **백업 / 복원.** 다섯 상태 저장소; 각각의 절차 문서화:
  - Postgres (세션): 표준 `pg_dump` / `pgBackRest`.
  - `ADK_CC_TASKS_DIR` (task): rsync / 볼륨 스냅샷.
  - `ADK_CC_CREDENTIAL_STORE_DIR` (자격 증명): rsync; **Fernet 키도 별도로 백업** — 암호화 blob은 키 없이 무용.
  - `ADK_CC_TENANT_REGISTRY_DIR` (mcp / skill 레지스트리): rsync.
  - `ADK_CC_TENANT_SKILLS_DIR` (skill 폴더): rsync.
  - 워크스페이스 (sandbox VM의 `/var/lib/adk-cc/wks`): 테넌트 SLA당 볼륨 스냅샷.
- ✗ **로그 로테이션.** `ADK_CC_AUDIT_LOG`은 영원히 append. `logrotate` 사용 (size 기반 또는 daily, open fd가 계속 쓰도록 copytruncate 사용).

### 다중 테넌트

- ✓ **Tenant 스코핑.** 워크스페이스, 세션, task, plan, MCP, skill, 자격 증명 모두 `tenant_id`별 스코프.
- ✓ **Tool-call rate cap.** `ADK_CC_QUOTA_PER_MINUTE`를 통한 per-tenant.
- ⚠️ **Per-session 자원 제한.** Sandbox container는 `ADK_CC_SANDBOX_*` env 변수당 고정 mem/cpu/pids — 모든 테넌트에 동일. 테넌트 티어별 차등 제한은 `DockerBackend` 서브클래싱 필요.
- ✗ **저장소 quota.** 워크스페이스 크기, plan 히스토리 깊이, task 카운트 — 오늘 무제한. 잘못 동작하는 세션이 `/var/lib/adk-cc/wks/<tenant>/<session>/`을 임의로 채울 수 있음.
- ✗ **Tenant 라이프사이클.** Onboarding (워크스페이스 + tenant dir 프로비전), offboarding (모든 artifact 삭제), GDPR 삭제 없음. 운영자가 문서화된 파일시스템 레이아웃에 대해 스크립트.
- ✗ **LLM 토큰 예산.** 소비된 LLM 토큰에 per-tenant cap 없음. 비용 폭주 가능. LiteLLM 후크를 통해 토큰을 추적하고 quota를 trip하는 `LlmCostPlugin` 추가.

### 구성

- ✓ **Env 주도 config.** `.env.example`이 모든 knob 문서화.
- ✗ **시작 검증.** 잘못된 값 (오타 env, 잘못된 permission YAML, 도달 불가 Postgres)이 종종 첫 요청에서만 surface. `make_app`에 eager probe 추가 — Postgres 연결, JWKS fetch, Docker 데몬 ping — 부팅에서 fail fast.

### 테스트 / CI

- ⚠️ **e2e.** `tests/e2e_features.py`이 JWT 인증, MCP admin + resolver, skill upload + resolver를 cover. FastAPI TestClient를 통한 in-process 실행. 아직 pytest 모양 아님, CI runner 없음.
- ✗ **Unit 테스트.** 커밋 내부 ad-hoc smoke 테스트; 영속 suite 없음. pytest로 `tests/unit/test_*.py`에 승격.
- ✗ **CI.** GitHub Actions / GitLab CI 등 없음. 기본 파이프라인 와이어: `uv sync`, `uv run pytest`, `pip-audit`, 선택적 `ruff` / `mypy`.
- ✗ **회귀 fixture.** Tool-call roundtrip용 stub MCP 서버 없음; skill 실행용 model-deterministic harness 없음. `tests/e2e_features.py`의 "What's NOT covered in-process" 노트 참고.

## Day-2 ops

### 흔한 로그 라인

| 어디서 | 의미 |
|---|---|
| `RuntimeError: ADK_CC_AGENTS_DIR must be set for make_app()` | Env var 누락; 시작 거부. |
| `RuntimeError: make_app(): no auth extractor configured` | `ADK_CC_JWT_JWKS_URL` (prod) 또는 `ADK_CC_AUTH_TOKENS` (dev 전용) 설정. |
| `EncryptedFileCredentialProvider needs a Fernet key` | `ADK_CC_CREDENTIAL_KEY` 설정. |
| `SandboxViolation: refusing to exec in prod-shaped path` | 프로덕션의 NoopBackend. `ADK_CC_SANDBOX_BACKEND=docker`로 전환. |
| `TenantMcpToolset: skipping server '<name>' for tenant '<id>'` | MCP 서버 도달 불가 / 잘못 구성. 테넌트의 등록된 URL + 자격 증명 확인. |
| `503 jwks fetch failed` | IdP JWKS endpoint 도달 불가. Egress 확인. |

### 사고: HITL 확인 중 stale-session 에러

증상: `sqlite_session_service.py:386`에서 `ValueError: The last_update_time provided in the session object is earlier than the update_time in storage`. 일반적으로 여러 turn 후, 특히 tool-confirmation pause/resume 간(예: `enter_plan_mode` / `exit_plan_mode` / permission 엔진을 통해 확인된 `run_bash`)에 발생.

근본 원인은 upstream: ADK의 pause/resume 사이클이 SSE 생성자의 in-memory 세션 참조를 갱신하지 않는 코드 경로를 통해 SQLite의 `update_time`을 bump. 런타임 핸들(`sandbox_backend`, `sandbox_workspace`, `tenant_context`)을 `temp:`로 prefix하여 ADK가 `extract_state_delta`에서 skip하도록 하여 우리의 상태 쓰기 기여를 mitigate. 남은 churn은 resume 동안 ADK 자체의 상태 변경에서.

**Dev 임시 해결책 #1 (no persistence)**: SQLite 세션 저장소 우회:

```bash
adk web . --session_service_uri=memory://
```

In-memory 세션은 optimistic locking 없음 → no stale-session 경쟁. 세션이 프로세스 재시작 시 사라짐.

**Dev / 프로덕션 임시 해결책 #2 (retry-on-stale)**: adk-cc가 `plugins/session_retry.py`에서 출하하는 session-retry wrapper에 opt in:

```bash
export ADK_CC_SESSION_RETRY_ON_STALE=1
```

모듈 import 시 `SqliteSessionService.append_event`와 `DatabaseSessionService.append_event` 패치. Stale-session ValueError에서 fresh 세션 fetch, `last_update_time` (와 `event_sequence`, PR-#4752 후) 동기화, append 한 번 재시도. 모든 재시도를 WARNING으로 로그. Single-retry 의미 — 두 번째 시도도 실패하면 raise (애플리케이션이 surface해야 하는 진정한 동시 writer 충돌).

이것은 SQLite와 Postgres에 모두 동작. Caveat:
- 실제 동시 writer가 재시도 후에도 자신의 event를 잃을 수 있음 — 의도된 동작 (하나의 writer만 승리).
- 재시도가 자주 발생하면, wrapper가 단순히 종이로 덮고 있는 underlying contention의 신호.

### 사고: orphan sandbox container

Agent pod 충돌에 의해 트리거. Sandbox VM에서 정리:

```bash
docker ps --filter label=adk-cc-session --format '{{.Names}} {{.Status}}'
# Up >1h인 것 reap: stop + remove
docker ps -q --filter label=adk-cc-session --filter status=running \
  | xargs -I {} sh -c 'docker stop {} && docker rm -v {}'
```

### 사고: 키 로테이션 후 자격 증명 복호화 실패

OLD 키에서 암호화된 blob은 NEW 키로 복호화 불가. 깔끔하게 로테이션하려면: 옛 키로 모든 blob 복호화 (프로그램 루프), 새 키로 재암호화, `ADK_CC_CREDENTIAL_KEY` swap 후 재시작. 자동화된 로테이션 도구는 아직 없음.

### 업그레이드

`uv sync` + 재시작이 adk-cc-internal 변경에 동작. 업그레이드 후 확인:

1. `tests/e2e_features.py` 통과.
2. 라이브 smoke test (위 step 8).
3. Smoke test 동안 sandbox VM에서 `docker ps` 확인 — per-session container가 여전히 정상적으로 spawn하고 사라져야.

릴리스가 상태 저장소 디스크 형식 변경 (task JSON, 자격 증명 blob, plan 파일)하면: 릴리스 노트가 이를 명시하고 migration 스크립트 제공. 오늘의 형식:

- Tasks: `ADK_CC_TASKS_DIR/<tenant>/<session>/<task_id>.json` — Pydantic 스키마는 `adk_cc/tasks/model.py` 참고.
- 자격 증명: `<store_dir>/<tenant>/<key>.enc` — Fernet ciphertext.
- Tenant 레지스트리: `<registry_dir>/<tenant>/mcp.json` — `McpServerConfig`의 JSON 목록.
- Skills: `<skill_root>/<tenant>/<name>/SKILL.md` (+ scripts) — ADK skill 형식.
- Plans: `<workspace>/.adk-cc/plans/<timestamp>-<slug>.md` — Markdown.
