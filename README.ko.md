# adk-cc

Claude Code 스타일의 **gather → plan → act → verify** 에이전트 루프를 단일 ADK 에이전트 모듈로 구현한 프로젝트입니다. `adk web` / `adk run`으로 바로 실행할 수 있고, 단일 인스턴스 배포용 FastAPI 팩토리와 함께 사용할 수 있는 커스텀 React 챗 UI도 포함되어 있습니다.

읽기: [English](./README.md) · **한국어**

자세한 문서는 [`docs/`](./docs/) 아래에 있습니다: [스펙](./docs/01-specification.md), [아키텍처](./docs/02-architecture.md), [프롬프트](./docs/03-prompts.md), [샌드박스 런북](./docs/04-deployment-sandbox.md), [프로덕션 런북](./docs/05-production-deployment.md), [확인(confirmation) 와이어 프로토콜](./docs/06-confirmation-protocol.md), [웹 UI](./docs/07-web-ui.md).

## 개요

- **Coordinator 한 명**이 사용자와 직접 대화하는 유일한 에이전트입니다. 직접 read 도구(`read_file`, `glob_files`, `grep`, `web_fetch`, `read_current_plan`), write/exec 도구(`write_file`, `edit_file`, `run_bash`), task 도구(`task_create` / `task_get` / `task_list` / `task_update`), HITL 도구(`ask_user_question`), plan-mode 도구(`enter_plan_mode`, `exit_plan_mode`, `write_plan`), 자동 로딩된 skill/MCP toolset을 사용합니다.
- **두 명의 specialist**가 ADK `sub_agents`로 연결되어 있습니다: **`Explore`** (코드베이스 광범위 탐색 후 보고서 작성)와 **`verification`** (구현 후 검증, `VERDICT: PASS|FAIL|PARTIAL`로 종료). Delegation은 `transfer_to_agent`로 이루어지고, sub-agent는 부모의 invocation context를 공유하므로 모든 tool call/response가 불투명한 tool result에 묻히지 않고 UI에 스트리밍됩니다.
- **Planning은 sub-agent가 아닙니다.** 사용자가 변경 전에 승인 가능한 written plan을 원할 때 coordinator가 `enter_plan_mode`를 호출합니다 — 이는 coordinator가 취하는 posture입니다. 그러면 `PlanModeReminderPlugin`이 LLM의 tool surface에서 write/exec 도구를 동적으로 필터링하고 planning instruction을 주입합니다. Plan은 `write_plan`으로 영속화되고, `exit_plan_mode`가 사용자 승인 후에야 제약 없는 surface로의 재진입을 허용합니다.
- Hub-and-spoke 구조 + "coordinator-owns-user-I/O" 원칙은 **두 가지** ADK 메커니즘으로 강제됩니다 — 둘 중 하나만으로는 부족합니다:
  1. 각 specialist의 **`disallow_transfer_to_parent=True`** (cross-turn 구조적 보장 — 다음 사용자 메시지에서 누구의 turn인지 정할 때 runner가 specialist를 건너뜁니다).
  2. 합성 `function_call` part를 반환하는 **`after_agent_callback`** — `Event.is_final_response()`가 False가 되어 LLM flow가 한 번 더 반복되고, coordinator가 same-turn 최종 응답을 전달합니다.
- `disallow_transfer_to_peers=True`로 specialist→specialist 이동을 차단합니다.
- Specialist의 tool denylist는 write 도구를 단순히 넘기지 않는 **구조적** 방식으로 강제됩니다.
- `ToolCallValidatorPlugin`이 런타임에 "Tool not found" 에러(예: plan mode에서 필터링된 도구를 모델이 호출한 경우)를 잡아 corrective response를 반환하므로, 모델이 turn을 중단하지 않고 다음 iteration에서 self-correct할 수 있습니다.

## 디렉터리 구조

```
adk-cc/                           ← repo 루트
├── pyproject.toml                ← packages.find where=["agents"] → adk_cc 설치
├── Dockerfile.sandbox            ← per-session sandbox 이미지
├── .env.example                  ← 전체 설정 surface (~65개 ADK_CC_* 변수)
├── docs/                         ← 아키텍처, 프롬프트, 배포 런북
├── scripts/                      ← 운영자 CLI + skill/context/compaction 데모
├── tests/                        ← unit + e2e 테스트 ("Tests" 절 참고)
├── web/                          ← React 챗 UI (Vite + Tailwind v4 + shadcn/ui)
│   └── src/{api,components,pages,lib}
└── agents/                       ← AGENTS_DIR (ADK가 여기서만 에이전트 발견)
    └── adk_cc/                   ← 에이전트 패키지 (`adk_cc`로 import)
        ├── __init__.py           ← .env bootstrap + `from . import agent`
        ├── agent.py              ← `app`(권장)과 `root_agent` export
        ├── prompts.py            ← 에이전트별 instruction
        ├── logging_setup.py      ← ADK_CC_LOG_* 설정
        ├── tools/                ← AdkCcTool 서브클래스 (read/write/exec/task/HITL/plan/skills/MCP)
        ├── plugins/              ← ADK BasePlugin 통합
        ├── permissions/          ← 규칙 엔진 + 확인 페이로드
        ├── sandbox/              ← SandboxBackend ABC + 구현
        │   └── backends/{noop,docker,sandbox_service,daytona,e2b}.py
        ├── tasks/                ← task 추적 + JSON 저장소 (filelock 안전)
        ├── credentials/          ← per-tenant 시크릿 저장소
        └── service/              ← FastAPI 팩토리 + 인증 + 테넌시 + admin
```

`agents/`는 ADK의 `AGENTS_DIR`입니다 — 에이전트 패키지만 담고 있어 로더가 에이전트만 발견합니다(`web/`, `docs/`, `tests/` 제외). 패키지는 top-level `adk_cc`로 설치·import됩니다(setuptools `where=["agents"]`); uvicorn 팩토리는 `adk_cc.service.server:make_app`.

## 빠른 시작

```bash
cd adk-cc
uv venv .venv && source .venv/bin/activate
uv pip install -e .

# .env — 최소한 모델 서버의 API 키
echo 'ADK_CC_API_KEY=sk-your-model-server-key' > .env

# 옵션 A: ADK 번들 웹 UI (agents/ 디렉터리를 가리킴)
adk web agents

# 옵션 B: 한 번만 실행 (CLI)
adk run agents/adk_cc

# 옵션 C: FastAPI + 커스텀 React UI ("웹 UI" 절 참고)
```

## 웹 UI

[`web/`](./web/) 아래에 커스텀 React 챗이 있습니다. 번들 `adk web` UI 대신 사용자용 채팅에 사용하며, adk-cc 특유의 위젯들을 제공합니다: confirmation 프롬프트, 구조화된 `ask_user_question` 폼, plan/edit/bash artifact 렌더러, task sidebar, slash 명령어, 테마, SSE 토큰 스트리밍.

### 실행 방법

```bash
# 1. 번들 빌드 (한 번만, web/ 변경 시 다시 빌드)
npm --prefix web install
npm --prefix web run build

# 2. FastAPI 서버를 UI 마운트와 함께 시작
ADK_CC_AGENTS_DIR=$(pwd)/agents \
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
ADK_CC_SERVE_UI=1 \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# 3. http://127.0.0.1:8000/ 접속 후 토큰 `devtok`으로 로그인
```

`adk_cc` 패키지가 import 시점에 `.env`를 자동 로드합니다
(`ADK_CC_AGENTS_DIR` → repo root → CWD 순). 따라서
`set -a; . ./.env; set +a` 없이도 `.env`의 `ADK_CC_API_KEY` /
`ADK_CC_MODEL` / `ADK_CC_API_BASE`이 uvicorn에 도달합니다.
프로세스 env가 `.env`보다 우선합니다. 비활성화하려면
`ADK_CC_SKIP_DOTENV=1`.

같은 서버에 대해 HMR 개발을 하려면 `npm --prefix web run dev`를 사용하세요 (Vite dev server가 `:5173`에서 `/run*`, `/apps`, `/list-apps`, `/api`, `/admin`, `/debug`을 기본적으로 `http://127.0.0.1:8000`으로 프록시; `ADK_CC_DEV_API`로 오버라이드).

### UI 구성요소

- **Session rail** — 왼쪽 내비게이션: 에이전트 picker (`/list-apps`) + 사용자별 세션 목록 + 새 세션/삭제.
- **Thread** — ADK 이벤트를 채팅 행으로 평탄화합니다. 토큰을 도착하는 즉시 스트리밍 (`/run_sse`의 `streaming: true` 옵트인, chunk delta 누적).
- **Composer** — 다중 행 textarea. Enter 전송, Shift+Enter 줄바꿈. `/`를 누르면 slash 명령어 picker가 열립니다.
- **adk-cc 인지 위젯** (pending 상태에서는 generic tool-call 카드 대신 표시):
  - `ConfirmationCard` — permission 엔진의 "ask" 분기에서 보낸 `ConfirmPrompt` 페이로드를 렌더링 (allow once / allow always / deny + 선택적 코멘트 + persist 토글). `adk_request_confirmation` / `adk_cc_confirmation_form` function-call로 트리거.
  - `AskUserQuestionCard` — 구조화된 단일/다중 선택 폼. `ask_user_question` long-running call로 트리거; 자유형 "Other" 옵션을 자동 추가.
- **Artifact 렌더러** (call + response를 하나의 카드로 묶음):
  - `BashTerminalCard` (`run_bash`) — `$ command` 프롬프트, stdout/stderr 컬러링, exit-code 칩.
  - `FileEditCard` (`edit_file`, `write_file`) — 편집은 좌우 before/after 비교, 쓰기는 단일 녹색 블록.
  - `PlanCard` (`write_plan`, `read_current_plan`) — markdown 본문 + 저장 경로 + 접기/펴기 가능한 히스토리.
- **Task sidebar** — `task_create` / `task_update` / `task_list` function-response로부터 실시간 task 목록을 도출 (별도 엔드포인트 없음).
- **Plan mode** — `session.state.permission_mode === "plan"`일 때 composer에 보라색 배지 + 테두리 색상이 적용되어 입력 순간 현재 모드를 분명히 알 수 있습니다.
- **Slash 명령어** — `/help`, `/clear` (새 세션), `/plan`과 `/exit-plan` (`PATCH /apps/.../sessions/{id}`에 `state_delta`를 보내 `permission_mode`를 직접 전환 — 결정적이며 LLM round-trip 없음), `/theme` (light → dark → system 순환), `/settings`, `/signout`.
- **Settings 다이얼로그** — 가벼운 모달 (Radix 의존성 없음). 테마 picker(light/dark/system), 읽기 전용 identity 행, sign-out 단축. 헤더의 기어 아이콘 또는 `/settings`로 접근.

### 인증 및 서빙

Auth middleware는 SPA 번들 경로(`/`, `/favicon.svg`, `/assets/*`)를 면제하여 로그인 폼이 익명으로 로드될 수 있게 합니다. 나머지(`/run*`, `/apps/*`, `/list-apps`, `/debug/*`)는 모두 게이트됩니다. JWT(`JwtAuthExtractor`)와 dev 토큰 맵(`BearerTokenExtractor`) 모두 동작합니다 — React 앱은 단순히 Bearer 헤더를 붙여 POST합니다.

UI 관련 env 변수:

```bash
ADK_CC_SERVE_UI=1                  # web/dist의 SPA를 /에 마운트
ADK_CC_UI_DIST=/path/to/web/dist   # 기본값 (<repo>/web/dist) 오버라이드
ADK_CC_DEV_API=http://...:8000     # dev 전용, Vite proxy용
```

## 로컬 모델

ADK의 `LiteLlm` 래퍼 아래에서 LiteLLM을 사용하며, OpenAI 호환 서버를 가리킵니다.

**기본값**:
- model: `openai/Qwen3.6-35B-A3B-UD-MLX-4bit`
- api base: `http://localhost:18000/v1`
- api key: `ADK_CC_API_KEY`에서 읽음 (필수)

코드 변경 없이 env로 오버라이드 가능:

```bash
ADK_CC_MODEL=openai/<model-id>          # 예: openai/qwen2.5-coder-32b
ADK_CC_API_BASE=http://host:port/v1
ADK_CC_API_KEY=<token>
```

**function-calling 지원** 모델을 선택하세요 — 이 루프는 tool use에 의존합니다. Qwen 2.5+, Llama 3.1/3.2, Mistral 계열 모두 동작합니다. 소형(1B–3B) 모델은 tool call을 잘 다루지 못하는 경우가 많습니다.

전체 설정 surface는 [`.env.example`](./.env.example)을 참고하세요.

## Sandbox 백엔드

호스트를 건드리는 모든 도구(`run_bash`, `read_file`, `write_file`, `edit_file`, skill script)는 `SandboxBackend`를 통해 라우팅됩니다. `ADK_CC_SANDBOX_BACKEND`로 선택:

| 백엔드 | 언제 사용 | 격리 수준 |
|---|---|---|
| `noop` (기본) | 로컬 `adk web .` 개발 | 없음 — 호스트에서 실행. `ADK_CC_NOOP_ACK_HOST_EXEC=1` 없이는 prod-shaped 경로 거부 |
| `docker` | Docker 데몬을 직접 운영하는 단일 인스턴스 프로덕션 | Per-session 컨테이너, read-only rootfs, cap drop, mem/cpu/pids 제한, 기본 `network_mode=none` |
| `sandbox_service` | 에이전트 프로세스에 Docker 데몬 권한을 주고 싶지 않을 때 | 외부 REST 샌드박스 서비스([JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing) 또는 호환)로 위임 — gVisor + cap-drop + read-only rootfs + userns-remap + Squid egress allowlist |
| `e2b` | (스텁) | 미래: hosted Firecracker microVM |

`docker`와 `sandbox_service`의 운영자 셋업은 [`docs/04-deployment-sandbox.md`](./docs/04-deployment-sandbox.md) 참고. `sandbox_service` 쪽이 가벼운 옵션(에이전트 호스트에 Docker 데몬 불필요)이고, `docker`는 in-process로 모든 것을 처리합니다.

### Streaming exec (운영자 관측성)

`ADK_CC_BASH_STREAM=1`로 설정하면 `run_bash` chunk가 도착하는 즉시 INFO로 로깅되어, 명령 완료를 기다리지 않습니다. 모델은 여전히 하나의 최종 결과를 받으며, 스트리밍은 에이전트 로그를 tail하는 운영자를 위한 것입니다. 현재 `sandbox_service`만 실제로 chunk를 라이브 스트리밍하며, `noop`/`docker`는 ABC 기본값(끝에서 한 chunk)을 사용합니다. `exec_stream()` 계약은 `adk_cc/sandbox/backends/base.py` 참고.

## Skills

Skill은 운영자 정의 파라미터화 프롬프트입니다 (Anthropic skill 포맷). adk-cc는 부팅 시 `adk_cc/skills/` (또는 `ADK_CC_SKILLS_DIR`)에서 skill을 자동 로드하고 네 개의 model-callable 도구로 노출합니다: `list_skills`, `load_skill`, `load_skill_resource`, `run_skill_script`.

Skill 스크립트는 `run_bash`와 동일한 `SandboxBackend`를 통해 실행됩니다 — 즉 `docker`/`sandbox_service`에서는 per-session 컨테이너 안에서 실행되고, `noop`에서만 호스트에서(샌드박스 우회) 실행됩니다.

```bash
ADK_CC_SKILLS_DIR=/path/to/skill-folders   # 기본값: adk_cc/skills/ (존재 시)
```

비표준 skill 레이아웃(예: `references/` 아래가 아닌 skill 루트에 doc 파일이 있는 경우)은 fallback `load_skill_resource`가 처리합니다 — ADK의 엄격한 path 조회가 빗나가면 파일시스템 스캔으로 대응.

프로젝트 레벨 skill도 작업 트리의 `.adk-cc/skills/`와 `.claude/skills/`에서 자동 발견됩니다 (`ADK_CC_DISABLE_PROJECT_SKILLS=1`로 비활성).

## 프로젝트 컨텍스트

`ProjectContextPlugin`이 모든 에이전트 호출의 `system_instruction`에 프로젝트 레벨 컨텍스트 파일을 자동 로드합니다. 검색 순서:

1. `.adk-cc/CONTEXT.md` (project-owned)
2. `CLAUDE.md` (Claude Code 관례)
3. `AGENTS.md` (Agent.md 관례)

먼저 매칭되는 것이 적용됩니다. `ADK_CC_DISABLE_PROJECT_CONTEXT=1`로 비활성, `ADK_CC_CONTEXT_FILES=path1,path2`로 검색 목록 오버라이드.

## MCP 서버

Per-tenant 레지스트리를 통해 외부 MCP 서버에 연결할 수 있습니다. `ADK_CC_TENANT_REGISTRY_DIR`을 설정하고 테넌트별 `mcp.json`을 저장하세요. `TenantMcpToolset`이 invocation마다 활성 테넌트의 설정에서 서버를 해석하고, 자격 증명은 credential provider에서 치환됩니다.

단일 테넌트 배포에서는 하나의 tenant_id를 사용해도 됩니다 (dev에서는 기본 `"local"`). 레지스트리/자격 증명 env 변수는 [`.env.example`](./.env.example) 참고.

## 단일 인스턴스 서버 배포

`adk web .`은 개발용으로 좋습니다. 장기 실행 단일 인스턴스 서버(예: dev VM, 신뢰할 수 있는 내부 팀, 단일 테넌트 배포)에는 FastAPI 팩토리를 사용하세요:

```bash
uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

`make_app`은 모든 것을 env에서 읽고([`.env.example`](./.env.example) 참고) 에이전트의 `App`이 이미 등록한 플러그인(`adk_cc/agent.py`가 `ProjectContext`, `AskUserQuestionUiHint`, `ConfirmationFormUi`, `ModelIOTrace`를 추가) 위에 프로덕션 전용 체인 `[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator, ContextGuard]`을 얹고 auth middleware를 와이어링합니다. `SessionRetryPlugin`은 `adk_cc/plugins/`에 존재하지만 opt-in입니다 — stale-session 복구가 필요하면 명시적으로 와이어링하세요. **인증은 fail-closed**: 다음 중 하나가 설정되지 않으면 시작을 거부합니다:

- `ADK_CC_AUTH_TOKENS=tok1=alice:tenant_a,tok2=bob:tenant_b` — 정적 토큰 맵 (단일 인스턴스, 간단)
- `ADK_CC_JWT_JWKS_URL=...` + `ADK_CC_JWT_ISSUER=...` 등 — JWT 검증
- `ADK_CC_ALLOW_NO_AUTH=1` — 명시적 dev 탈출구 (프로덕션에서 사용 금지)

### 최소 단일 테넌트 프로덕션 레시피

"서버 하나, 팀 하나, 영속 세션, 진짜 격리만 있으면 됨"이라면:

```bash
# .env (또는 프로세스 env)
ADK_CC_API_KEY=sk-your-model-key
ADK_CC_MODEL=openai/<your-model>
ADK_CC_API_BASE=http://your-model-server:18000/v1

# 정적 토큰 맵: 모두 tenant=internal
ADK_CC_AUTH_TOKENS=alice_token=alice:internal,bob_token=bob:internal

# Sandbox: 하나 선택
ADK_CC_SANDBOX_BACKEND=docker
ADK_CC_DOCKER_HOST=unix:///var/run/docker.sock        # local Docker
# OR
ADK_CC_SANDBOX_BACKEND=sandbox_service
ADK_CC_SANDBOX_SERVICE_URL=http://localhost:8000      # JISUlicious/sandboxing
ADK_CC_SANDBOX_SERVICE_SHARED_TOKEN=<bearer>

# 사용자별 영속 워크스페이스: <root>/<tenant>/<user>/
ADK_CC_WORKSPACE_ROOT=/var/lib/adk-cc/wks

# 영속 세션 (단일 인스턴스에는 sqlite 충분)
ADK_CC_SESSION_DSN=sqlite:////var/lib/adk-cc/sessions.db

# Audit 로그
ADK_CC_AUDIT_LOG=/var/log/adk-cc/audit.jsonl

# Permissions YAML (선택이지만 권장)
ADK_CC_PERMISSIONS_YAML=/etc/adk-cc/permissions.yaml

# 커스텀 UI (선택 — React 챗을 /에 번들)
ADK_CC_SERVE_UI=1
```

그리고:

```bash
uvicorn adk_cc.service.server:make_app --factory --host 0.0.0.0 --port 8000
```

이것만으로 다음을 갖춘 완전 동작 단일 인스턴스 배포가 됩니다:
- 세션별 실제 sandbox 격리
- 사용자별 영속 워크스페이스 (alice의 파일은 다음 세션에도 살아남음)
- 영속 세션 (프로세스 재시작 후에도 대화 기록 보존)
- 단일 테넌트의 정적 토큰 인증
- 모든 tool call의 감사 로그
- `/`에 마운트된 커스텀 React 챗 UI

다중 테넌트 배포와 전체 readiness checklist(security / reliability / observability / ops 갭)는 [`docs/05-production-deployment.md`](./docs/05-production-deployment.md) 참고.

## 테넌시

`tenant_id`는 인증(JWT 클레임 또는 정적 토큰 맵)에서 읽힙니다. `TenancyPlugin`이 첫 tool call에서 세션 상태를 lazy seed합니다:

```
state["temp:tenant_context"]    →  TenantContext(tenant_id, user_id, root)
state["temp:sandbox_workspace"] →  WorkspaceRoot(<root>/<tenant>/<user>/)
state["temp:sandbox_backend"]   →  per-session 백엔드 인스턴스
```

기본 resolver가 ContextVar를 통해 auth에서 추출된 tenant_id를 플러그인 계층으로 브리지합니다. 그래서 `ADK_CC_AUTH_TOKENS=tok=alice:acme`만으로 alice가 `tenant=acme`으로 스코프됩니다 — 커스텀 resolver 불필요. `user_id → tenant_id` 매핑 로직이 따로 있는 운영자는 `TenancyPlugin`에 `tenant_resolver=callable`을 제공할 수 있습니다.

`adk web .`은 항상 `tenant_id="local"`로 실행됩니다 (auth 없음 = 단일 테넌트 개발).

(테넌트, 사용자, 세션)별 영속화 방식은 [`docs/02-architecture.md`](./docs/02-architecture.md) §7.6(워크스페이스 레이아웃) 참고.

## Plan mode

변경 전에 사용자 승인이 필요한 written plan이 작업에 적합할 때, coordinator가 `enter_plan_mode(reason=...)`를 호출하고 세션이 plan mode로 들어갑니다(`permission_mode="plan"`). 그러면 `PlanModeReminderPlugin`이:

- LLM의 tool surface에서 write/exec 도구(`write_file`, `edit_file`, `run_bash`, `task_create`, `task_update`, `enter_plan_mode`)를 필터링.
- read 도구, `write_plan` / `read_current_plan` / `exit_plan_mode` / `ask_user_question`, `Explore` sub-agent는 보이게 유지.
- Planning instruction 주입 (4단계: understand → explore → design → detail; `write_plan`의 필수 출력 형식).

Coordinator는 `write_plan`으로 plan을 생성하고(매 호출마다 `<workspace>/.adk-cc/plans/` 아래 타임스탬프 파일이 새로 만들어짐) `exit_plan_mode`로 turn을 종료하며, 사용자에게 명시적 승인을 요청합니다. 승인되면 `permission_mode`가 되돌아가고 write 도구가 다시 등장합니다.

Plan mode는 `exit_plan_mode`와 비대칭입니다: 진입은 posture를 조이고(확인 불필요), 종료는 완화하므로(사용자 승인 필수).

웹 UI는 `/plan`과 `/exit-plan` slash 명령어도 노출합니다 — `PATCH /apps/.../sessions/{id}`에 `state_delta`를 보내 `permission_mode`를 직접 전환하므로 결정적이고 LLM round-trip을 건너뜁니다.

## Confirmation

파괴적 tool call(및 모든 ASK-rule 매칭)은 ADK의 `request_confirmation` seam을 통해 사용자 확인을 위해 일시 정지됩니다. `PermissionPlugin`이 세 가지 옵션의 구조화된 `ConfirmPrompt` 페이로드를 보냅니다 — **Allow once / Allow always / Deny**. "Allow always"는 `(tool, 추출된 rule key)`로 키된 SESSION 스코프 ALLOW 규칙을 주입하여 세션 내내 동일 작업이 다시 묻지 않도록 합니다; 스코프는 의도적으로 좁게(정확한 rule-key 매치, 와일드카드 없음) 유지됩니다.

`ConfirmationFormUiPlugin`(기본 등록)이 이를 번들 `adk web` UI에 브리지하여 옵션들이 하드코딩된 이진 체크박스가 아닌 선택 가능한 폼으로 렌더링됩니다. `web/`의 커스텀 React UI는 구조화된 페이로드를 직접 읽고(`ConfirmationCard.tsx`) `chose_id` / `comment` / `persist_across_sessions` 응답을 회신합니다 — 이 경로에서는 rewrite 플러그인 불필요.

와이어 컨트랙트: [`docs/06-confirmation-protocol.md`](./docs/06-confirmation-protocol.md).

## Tasks

순수 추적용 `task_*` 도구 네 개(`task_create` / `task_get` / `task_list` / `task_update`) — 실행 의미는 없음. Task는 `<workspace>/.adk-cc/tasks/<session_id>/<task_id>.json`에 JSON 파일로 영속화됩니다 (root는 `ADK_CC_TASKS_DIR`로 오버라이드). Task는 프로세스 재시작 후에도 살아남고, `filelock` 쓰기로 멀티 워커 배포에서도 안전합니다.

세 가지 상태: `pending`, `in_progress`, `completed`. Plan mode에서는 task 도구가 필터링됩니다 (task는 act-time 진행 체크리스트이지 planning surface가 아님).

`TaskReminderPlugin`이 활성 task 목록을 모델 컨텍스트에 주기적으로 주입합니다 — 모델이 `task_create`/`task_update` 없이 너무 많은 turn(기본 10)을 보냈고 마지막 리마인더로부터 최소 그만큼의 turn이 지났을 때(기본 10) 발화:

```bash
ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE=10
ADK_CC_TASK_REMINDER_TURNS_BETWEEN=10
```

웹 UI는 세션 이벤트 로그의 `task_*` function-response로부터 도출한 라이브 task 목록을 우측 사이드바 `TaskSidebar`로 표시합니다.

## 테스트

```
tests/
├── test_*.py                          ← unit 테스트 (mock I/O, 빠름; ~126개 체크)
│   ├── test_sandbox_service_backend.py
│   ├── test_workspace_layout.py
│   ├── test_skill_resource_fallback.py
│   ├── test_session_retry.py
│   ├── test_context_guard.py
│   ├── test_tenancy_resolver.py
│   ├── test_permissions_confirmation.py
│   ├── test_ask_user_question_ui_hint.py
│   ├── test_confirmation_form_ui.py
│   ├── test_plan_mode_env_default.py
│   ├── test_plan_mode_tools_env_default.py
│   ├── test_read_file_limits.py
│   ├── test_token_counter.py
│   ├── test_logging_setup.py
│   ├── test_model_io_trace.py
│   ├── test_audit_extensions.py
│   ├── test_project_context.py
│   └── test_compaction_audit.py
│
├── e2e_features.py                    ← in-process FastAPI e2e (auth + admin + skill 업로드)
├── e2e_confirmation_flow.py           ← in-process ADK Runner e2e — confirmation gate, allow_always 세션 규칙, deny 경로, scope-narrow 체크
├── e2e_confirmation_form_ui.py        ← in-process ADK Runner e2e — sentinel name rewrite, form-shaped resume, form widget으로 deny
├── e2e_ask_user_question.py           ← in-process ADK Runner e2e — long_running pause, premature response 없음, 사용자 답변으로 resume
│
└── 라이브 sandbox service 대상 e2e:
    ├── e2e_sandbox_service.py         9 contract 체크 + 6 버그 수정 검증
    ├── e2e_sandbox_comprehensive.py   9 카테고리 53개 체크
    ├── e2e_skills.py                  6 — 전체 skill chain
    ├── e2e_streaming_adapter.py       9 — exec_stream + BashTool 스트림
    └── diag_streaming_timing.py       진단 (always-on probe)
```

Unit 테스트는 env 설정 없이 실행:

```bash
.venv/bin/python tests/test_sandbox_service_backend.py
.venv/bin/python tests/test_workspace_layout.py
# ... 기타
```

라이브 sandbox service 대상 e2e (실행 중인 JISUlicious/sandboxing 인스턴스를 가리킴):

```bash
ADK_CC_SANDBOX_SERVICE_URL=http://127.0.0.1:8000 \
SANDBOX_API_TOKEN=<token> \
  .venv/bin/python tests/e2e_sandbox_comprehensive.py
```

`e2e_skills.py`와 `e2e_streaming_adapter.py`는 Python 3.12+와 adk-cc import 가능성을 요구합니다; 사전 reachability 체크 후 안 되면 깔끔하게 skip합니다.

## 상태

**Alpha** (`v0.0.1`이 첫 태그 릴리스). 28개의 테스트 파일(20개 unit + 8개 e2e/diagnostic)로 end-to-end 검증 완료, `feat/chat-ui`에서 동작하는 React 챗 UI 포함. [`docs/05-production-deployment.md`](./docs/05-production-deployment.md)의 readiness checklist(security / reliability / observability / ops / 다중 테넌트 / config / tests)에 명시된 운영 갭이 있습니다. 실제 사용자에게 서비스하기 전에 위협 모델과 SLO에 맞춰 ✗ 항목을 닫으세요.
