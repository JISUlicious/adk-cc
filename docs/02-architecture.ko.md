# 아키텍처 (Architecture)

## 1. 파일 레이아웃

에이전트 패키지는 `agents/adk_cc/`에 있습니다. ADK의 `AGENTS_DIR`은 `agents/` 디렉터리(`adk web`이 가리킬 경로)로, 에이전트 패키지만 담고 있어 로더가 `web/`, `docs/`, `tests/`를 가짜 app으로 노출하지 않습니다. setuptools가 `where=["agents"]`를 사용하므로 패키지는 여전히 top-level `adk_cc`로 설치·import됩니다. 아래 트리는 (`agents/adk_cc/`에 루팅된) 패키지 내부를 보여줍니다:

```
adk-cc/                          ← repo 루트
├── pyproject.toml               # google-adk==1.31.1, litellm>=1.50; where=["agents"]
├── README.md
├── docs/                        # 이 디렉터리
├── web/                         # React 챗 UI
└── agents/                      # ← AGENTS_DIR (`adk web`이 가리킬 경로)
    └── adk_cc/                  # ADK가 발견하는 에이전트 패키지 (`adk_cc`로 import)
    ├── __init__.py              # `from . import agent`
    ├── agent.py                 # `app`(권장)과 `root_agent` export
    ├── prompts.py               # 에이전트별 system prompt
    ├── tools/                   # AdkCcTool 서브클래스 (Stage A) + 통합 (Stage E)
    │   ├── base.py              # AdkCcTool, ToolMeta
    │   ├── schemas.py           # Pydantic input model
    │   ├── _fs.py               # workspace-aware path resolver (Stage C)
    │   ├── read_file.py
    │   ├── glob_files.py
    │   ├── grep.py
    │   ├── write_file.py
    │   ├── edit_file.py
    │   ├── bash/{tool,prompt}.py
    │   ├── web_fetch.py         # preapproved-hosts URL fetcher (Stage E)
    │   ├── ask_user_question.py # long-running 다중 선택 HITL (Stage E)
    │   ├── enter_plan_mode.py   # permission_mode를 "plan"으로 전환
    │   ├── exit_plan_mode.py    # 사용자 승인 게이트; default로 되돌림
    │   ├── write_plan.py        # plan을 Markdown artifact로 영속화
    │   ├── read_current_plan.py # session state에서 최신 plan 읽기
    │   ├── mcp.py               # make_mcp_toolset() 팩토리 (Stage E)
    │   ├── skills.py            # SkillToolset 자동 로더 (Stage E)
    │   └── task/                # 4개 task 도구 (Stage F — 추적 전용)
    │       ├── create.py
    │       ├── get.py
    │       ├── list.py
    │       └── update.py
    ├── skills/                  # 선택: 여기의 skill 폴더가 자동 로드됨
    ├── permissions/             # 규칙 엔진 (Stage B)
    │   ├── modes.py             # PermissionMode enum
    │   ├── rules.py             # PermissionRule + 도구별 fnmatch matcher
    │   ├── settings.py          # SettingsHierarchy (policy/user/project/session)
    │   └── engine.py            # decide() — 4단계 flow
    ├── sandbox/                 # 격리 백엔드 (Stage C)
    │   ├── config.py            # FsRead/Write/NetworkConfig + ExecResult
    │   ├── workspace.py         # WorkspaceRoot — per-(tenant,session) FS root
    │   └── backends/
    │       ├── base.py          # SandboxBackend ABC
    │       ├── noop_backend.py  # 호스트 실행 (개발 전용); asyncio.subprocess로 async
    │       ├── docker_backend.py # remote-Docker per-session container (Stage C)
    │       └── e2b_backend.py   # hosted Firecracker microVM 스텁 (미래)
    ├── tasks/                   # task 추적 (Stage F — 순수 추적, 실행 없음)
    │   ├── model.py             # Task, TaskStatus (3 상태), blocks/blocked_by
    │   ├── storage.py           # TaskStorage ABC + InMemoryTaskStorage + JsonFileTaskStorage
    │   └── runner.py            # TaskRunner — thin storage facade (기본: JsonFileTaskStorage)
    ├── plugins/                 # ADK BasePlugin 통합
    │   ├── permissions.py       # PermissionPlugin (Stage B)
    │   ├── audit.py             # AuditPlugin (Stage D) — JSONL 또는 callable sink
    │   ├── plan_mode.py         # PlanModeReminderPlugin — 동적 tool 필터 + planning instruction
    │   ├── task_reminder.py     # TaskReminderPlugin — 주기적 task-list system reminder
    │   ├── quotas.py            # QuotaPlugin (Stage G) — per-tenant rate cap
    │   ├── tool_call_validator.py # "tool not found"를 corrective tool response로 변환
    │   └── context_guard.py     # context-length overflow의 pre-flight WARN/REJECT
    ├── service/                 # web-service 배포 (Stage G)
    │   ├── tenancy.py           # TenantContext + TenancyPlugin (state seeder)
    │   ├── auth.py              # AuthExtractor protocol + BearerTokenExtractor
    │   └── server.py            # build_fastapi_app() + make_app() 팩토리
    └── config/
        └── settings_loader.py   # YAML → SettingsHierarchy (Stage G)
```

**2026-05 노트:** `web/` 디렉터리(React 챗 UI)와 두 개의 추가 플러그인(`project_context.py`, `model_io_trace.py`, `session_retry.py`, `ask_user_question_ui.py`, `confirmation_form_ui.py`)이 위 트리 이후 추가되었습니다. 자세한 내용은 [07-web-ui.ko.md](./07-web-ui.ko.md)와 [README.ko.md](../README.ko.md) 참고.

`adk web` / `adk run`은 먼저 `app`을 찾고 그 다음 `root_agent`를 찾습니다. Stage B는 `app = App(name=..., root_agent=root_agent, plugins=[PermissionPlugin(...)])`을 추가하여 플러그인 체인이 자동으로 와이어링되게 합니다; `root_agent`의 직접 import(예: 테스트용)는 변경 없이 계속 동작합니다.

ADK의 `adk web` / `adk run`은 AGENTS_DIR의 직접 자식 디렉터리에서 `__init__.py`와 `agent.py`를 찾습니다. `agent.py`의 모듈 레벨 이름 `root_agent`가 진입 에이전트입니다.

## 2. 에이전트 토폴로지

```
coordinator (LlmAgent, root)
│   tools: read/write/exec + task tools + plan-mode tools
│          + ask_user_question + web_fetch + read_current_plan + write_plan
│          + (자동 로드) skills, MCP toolset
└── sub_agents:
    ├── Explore         (LlmAgent, read-only)  tools: read_file, glob_files, grep, web_fetch
    └── verification    (LlmAgent, /tmp 전용)  tools: read_file, glob_files, grep, run_bash, web_fetch, read_current_plan
```

Hub-and-spoke. Coordinator만 사용자와 대화하는 유일한 에이전트; specialist는 leaf. Planning은 specialist가 **아님** — coordinator가 `enter_plan_mode`를 호출하면 `PlanModeReminderPlugin`이 LLM의 tool surface에서 write/exec/task 도구를 동적으로 필터링하고 planning instruction을 주입합니다. Coordinator가 그 자리에서 planning 에이전트가 됨; transfer 의식 없음. [§3.5](#35-plan-mode-as-coordinator-posture) 참고.

각 specialist는:

- `disallow_transfer_to_parent=True`
- `disallow_transfer_to_peers=True`
- `after_agent_callback=_force_coordinator_continuation`

Delegation은 ADK의 auto-injected `transfer_to_agent` 도구를 통해 이루어집니다. Coordinator의 prompt는 각 specialist를 `agent.name`으로 명명 — 그것이 routing table. `AgentTool` 래퍼 없음; specialist는 `sub_agents=[...]`로 와이어링되어 부모의 invocation context를 공유 (그래서 그들의 event가 `adk web`의 UI로 스트림됨).

## 3. Coordinator-owns-user-I/O (이중 메커니즘)

ADK의 기본값은 sub-agent가 사용자에게 절대 말을 걸지 않도록 강제하지 않습니다. 두 가지 별개의 메커니즘이 두 가지 실패 모드를 cover; **어느 하나만으로는 충분하지 않습니다**.

### 3.1 Cross-turn: `disallow_transfer_to_parent=True`

사용자가 다음 메시지를 보낼 때, ADK runner는 누구의 turn인지 결정해야 합니다. 관련 코드:

```python
# google/adk/runners.py — Runner._find_agent_to_run
for event in filter(_event_filter, reversed(session.events)):
    if event.author == root_agent.name:
        return root_agent
    if not (agent := root_agent.find_sub_agent(event.author)):
        continue
    if self._is_transferable_across_agent_tree(agent):
        return agent
return root_agent
```

`_is_transferable_across_agent_tree`는 `disallow_transfer_to_parent`가 `True`인 모든 에이전트에 대해 `False`를 반환. 따라서 specialist는 후보에서 **건너뛰어지고**, runner는 더 거슬러 올라가거나 root로 fall-through. 순효과: 다음 사용자 메시지는 항상 coordinator로 라우팅됨.

부수 효과: ADK의 auto-injected transfer instruction(`google/adk/flows/llm_flows/agent_transfer.py:_get_transfer_targets`)은 `disallow_transfer_to_parent=False`일 때만 부모를 transfer 타깃으로 나열함. `True`로 설정하면 specialist의 prompt도 부모를 전혀 언급하지 않음 — "되돌리기"의 유혹 없음.

### 3.2 Same-turn: `after_agent_callback`

단일 turn 내에서 specialist가 끝나면 ADK flow 루프는 계속할지 확인합니다:

```python
# google/adk/flows/llm_flows/base_llm_flow.py — BaseLlmFlow.run_async
while True:
    last_event = None
    async for event in self._run_one_step_async(...):
        last_event = event
        yield event
    if not last_event or last_event.is_final_response() or last_event.partial:
        break
```

Specialist의 마지막 event가 텍스트 전용 메시지면 `is_final_response()`가 `True` 반환 → 루프 break → 사용자가 specialist의 텍스트를 직접 봄. 우리는 그것을 원하지 않습니다.

`Event.is_final_response()`(`google/adk/events/event.py`)는 event가 function call을 가질 때 `False`를 반환합니다. 따라서 after-agent 콜백은 합성 `function_call`이 유일한 `Part`인 `Content`를 반환:

```python
# adk_cc/agent.py — _force_coordinator_continuation
def _force_coordinator_continuation(callback_context):
    return types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(
            name="_handback_to_coordinator",
            args={},
        ))],
    )
```

이 event는 final이 아님 → flow 루프 → coordinator의 LLM이 대화 기록(specialist의 보고서 포함)과 함께 다시 호출됨 → coordinator가 사용자 대면 응답 생성.

합성 호출 이름(`_handback_to_coordinator`)은 핸들러가 없음. 제어 신호이지 tool 호출이 아님. 대부분의 LLM은 dangling function call을 우아하게 처리하고 다음 step에서 텍스트로 응답.

### 3.3 둘 다 필요한 이유

- §3.1(cross-turn) 없으면, specialist가 마지막 non-user event author이고 transferable이면 다음 사용자 메시지가 specialist에 도달할 수 있음.
- §3.2(same-turn) 없으면, specialist의 텍스트 전용 final 메시지가 현재 turn의 보이는 응답으로 사용자에게 표시되고 coordinator가 합성할 기회를 영원히 갖지 못함.

## 3.5 Plan mode as coordinator posture

Plan mode는 세션 전역 플래그(`state["permission_mode"] == "plan"`)로, `enter_plan_mode`가 켜고 `exit_plan_mode`가 끕니다 (후자는 `ToolMeta.requires_user_approval=True`를 통해 사용자 승인 게이트).

플래그가 설정되어 있는 동안 `PlanModeReminderPlugin.before_model_callback`이 매 coordinator LLM 호출에서 두 가지를 수행:

1. **Tool surface 필터링.** `llm_request.tools_dict`와 각 `tool_obj.function_declarations` 양쪽에서 `write_file`, `edit_file`, `run_bash`, `task_create`, `task_update`, `enter_plan_mode`를 제거. (둘 다 모델에 feed되므로 하나만 필터링하면 도구가 누설됨.) `exit_plan_mode`는 plan mode가 아닐 때 필터링됨 (종료할 것이 없음).
2. **Instruction 주입.** Planning `<system-reminder>`을 `llm_request.config.system_instruction`에 append: 4단계 process (understand / explore / design / detail), 필수 `write_plan` 출력 형식, `exit_plan_mode` 승인 계약.

Coordinator의 tool 목록 자체는 변경되지 않음 — `agent.py`는 mode와 무관하게 동일한 16개 도구를 등록. 플러그인이 LLM이 turn마다 보는 것을 좁히는 유일한 메커니즘. 이것은:

- 세션마다 agent 재와이어링 없음, 별도 "planning agent" 인스턴스 없음.
- 모델은 볼 수 없는 것을 호출할 수 없음.
- 그래도 모델이 hidden tool 이름을 환각하면, `ToolCallValidatorPlugin`이 결과적인 "tool not found" 에러를 잡음 (§7 참고) 그리고 corrective tool response 반환 — 루프 계속, 모델 self-correct.

History 노트: 이전 설계는 planning을 `transfer_to_agent`로 호출된 `Plan` sub-agent를 통해 라우팅했음. 그 메커니즘이 `enter_plan_mode`와 겹쳐서 (둘 다 "plan하고 사용자 승인으로 act" 생산) 모델이 둘 다 하게 만들었음. 통합이 planning을 단일 posture로 collapse.

## 4. Tool denylist via tool surface

"verifier는 파일을 편집할 수 없다"라고 말하는 플러그인이나 후크는 없습니다. 각 에이전트의 `LlmAgent.tools=[...]`이 접근 가능한 함수를 단순히 나열:

| Agent | `tools=[...]` |
|---|---|
| coordinator | 전체 surface — read 도구, `write_file`, `edit_file`, `run_bash`, task 도구, plan-mode 도구 (`enter_plan_mode`, `exit_plan_mode`, `write_plan`, `read_current_plan`), `ask_user_question`, `web_fetch`, 그리고 자동 로드된 skill/MCP. `permission_mode == "plan"`일 때 `PlanModeReminderPlugin`이 이를 동적으로 좁힘. |
| Explore | `read_file, glob_files, grep, web_fetch` |
| verification | `read_file, glob_files, grep, run_bash, web_fetch, read_current_plan` |

`AgentTool`은 어느 `tools` 목록에도 **없음**. `disallow_transfer_to_peers=True`와 결합되어, specialist는 위임하거나 재귀할 수 없음.

Verifier는 `run_bash`를 가짐 (빌드/테스트 실행 필요), 하지만 그 prompt는 프로젝트 디렉터리 쓰기 금지와 `/tmp` 허용을 말함. 이는 prompt 강제이지 구조적이 아님 — 런타임 레벨에서, 잘못 동작하는 verifier는 어디든 쓸 수 있음.

## 5. Verification gate 계약

Verifier의 prompt는 final report가 다음 중 하나로 끝날 것을 요구:

```
VERDICT: PASS
VERDICT: FAIL
VERDICT: PARTIAL
```

Coordinator의 prompt는 다음을 지시:

- 대화 기록에서 이 라인을 읽음.
- `FAIL`: 수정, 원본 요청 + 수정과 함께 verification에 재-transfer. `PASS`까지 반복.
- `PASS`: verifier 보고서에서 2–3 명령을 재실행하여 spot-check.
- `PARTIAL`: 통과한 것과 검증할 수 없던 것 보고.

Verdict에 대한 **코드 레벨 파서 없음**. 계약은 prompt 규칙 쌍: verifier가 라인을 생성, coordinator가 그것을 act on. 아키텍처 선택은 Claude Code upstream 패턴을 미러 ([03-prompts.ko.md](./03-prompts.ko.md)에서 계보 참고).

## 5.5. Sandbox layer (Stage C — DockerBackend가 여기 도착)

Sandbox 계층(`adk_cc/sandbox/`)이 보안 경계입니다.
`SandboxBackend`는 다섯 작업을 가진 추상 계약입니다:
`exec`, `read_text`, `write_text`, `ensure_workspace`, `close`.
호스트를 건드리는 모든 도구(`run_bash`, `read_file`, `write_file`,
`edit_file`, `glob_files`, `grep`, `write_plan`, `read_current_plan`)가
이 계약을 통해 라우팅됨 — 호스트 FS / subprocess를 직접 건드리지 않음.

**구현체:**

- **`NoopBackend`** — 호스트 실행; Python 체크로 path / network config 존중.
  Dev 전용; 보안 경계 아님. 두 안전 가드: 프로덕션 형태 경로(`$HOME`,
  `/tmp`, OS tempdir 외부)에서의 exec를 `ADK_CC_NOOP_ACK_HOST_EXEC=1`
  설정 없이는 거부; 존재하지 않거나 디렉터리가 아닌 `cwd` 거부.
  `make_app`의 `ADK_CC_ALLOW_NO_AUTH`와 동일한 명시적 ack 패턴.
- **`DockerBackend`** — (보통 원격) Docker 데몬에 연결하고 각 세션을
  자체 컨테이너에서 실행. Linux namespace + cgroup + read-only rootfs
  + bind-mount 워크스페이스 + cap drop을 통한 실제 격리.
  **워크스페이스는 sandbox 호스트의 파일시스템에 상주**, agent pod의
  것이 아님; agent는 워크스페이스 파일을 Python `Path`로 절대 열지 않음.
- **`E2BBackend`** — 스텁. Hosted Firecracker microVM. 미래.
- **`SandboxServiceBackend`** — 외부 sandbox service에 대한 REST client
  ([JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing)
  또는 호환). 더 강한 격리(gVisor + cap-drop + read-only rootfs +
  userns-remap + Squid egress)와 agent 프로세스에서 sandbox 우려를
  분리, per-call HTTPS round-trip 비용. Per-session 볼륨은
  service의 `Limits.hard_destroy_ttl_s`(기본 24h) 후 와이프되므로
  이 백엔드의 per-user persistent 상태는 한정됨 — 운영자가 TTL을
  올리거나 long-lived 상태를 다른 곳에 푸시. `ADK_CC_SANDBOX_BACKEND=sandbox_service`로
  선택. 운영자 런북은 `docs/04-deployment-sandbox.ko.md` §6.
- (미래) `KubernetesBackend`, `ModalBackend`, `NsjailBackend`이
  같은 ABC를 통해 pluggable로 남음.

**프로덕션 배포 토폴로지:**

```
┌───────────────────────────┐         ┌─────────────────────────────┐
│  K8s cluster              │         │  Sandbox host (Linux VM)    │
│                           │         │                             │
│  ┌────────────────────┐   │ Docker  │  • Docker daemon            │
│  │ adk-cc agent pod   │───┼─TCP API─┤    (port 2375 plain or      │
│  │  - DockerBackend   │   │         │     2376 mTLS)              │
│  │    (remote client) │   │         │  • adk-cc-sandbox image     │
│  └────────────────────┘   │         │  • per-session containers   │
│                           │         │  • workspaces on local NVMe │
└───────────────────────────┘         │    /var/lib/adk-cc/wks/...  │
                                      └─────────────────────────────┘
```

**연결 모드** (backend init 시 env var로 선택):

| Mode | 언제 | `ADK_CC_DOCKER_HOST` | TLS env var |
|---|---|---|---|
| Unix socket | Agent와 Docker 같은 호스트 | `unix:///var/run/docker.sock` | unset |
| Plain TCP | 신뢰할 수 있는 내부 네트워크, 단일 테넌트 | `tcp://host:2375` | unset |
| mTLS TCP | 신뢰할 수 없는 세그먼트, 회사 정책 | `tcp://host:2376` | 세 개 모두 설정 |

**Per-session container** (ADK 세션당 하나, 첫 tool call 시 lazy-spawn,
세션 종료 시 teardown):

```python
client.containers.run(
    image="adk-cc-sandbox:latest",        # ADK_CC_SANDBOX_IMAGE로 설정 가능
    name=f"adk-cc-{session_id}",
    network_mode="none",                  # 기본 거부
    mem_limit="4g",
    cpu_quota=100_000,                    # 1 CPU
    pids_limit=256,
    read_only=True,                       # rootfs 불변
    tmpfs={"/tmp": "size=1g,mode=1777"},
    volumes={ws.abs_path: {"bind": "/workspace", "mode": "rw"}},
    user="1000:1000",
    cap_drop=["ALL"],
    security_opt=["no-new-privileges"],
)
```

**경로 변환.** 도구가 sandbox 호스트 경로(`<ws.abs_path>/foo`)를 전달;
백엔드가 워크스페이스 prefix를 stripping하고 `/workspace`를 prepend.
Agent는 sandbox 호스트의 파일시스템을 그 외에는 보지 않음 — 백엔드가 유일한 seam.

**라이프사이클.** `TenancyPlugin.before_tool_callback`이 세션의 첫 tool에서
`backend.ensure_workspace(ws)`을 호출; `DockerBackend`의 경우 이것이
일회성 helper container를 실행하여 sandbox VM에서 워크스페이스 dir을
`mkdir -p`. `TenancyPlugin.after_run_callback`이 세션 종료에
`backend.close()`을 호출 — per-session container를 중지 + 제거.

### 하드웨어 사이징 (sandbox VM, 단일 호스트)

100 user × tabular 워크로드 (50K row × ~1K col, pandas /
numpy / sklearn): 16 physical core, 96 GB RAM, 1 TB NVMe SSD.
~10 동시 세션 × 4 GB sandbox = 40 GB peak. Docker 데몬과 adk-cc
워크로드만 실행하는 Linux 호스트 — 다른 테넌트 없음.

~500 user 이상으로 확장하려면 sandbox VM을 추가하고 `session_id`
consistent hashing으로 세션 라우팅.

## 6. 로컬 모델 와이어링

ADK의 `LiteLlm` 래퍼(`google.adk.models.lite_llm.LiteLlm`)가 kwarg를 LiteLLM의 completion API로 전달합니다. `MODEL`은 한 번 구성되어 세 에이전트(coordinator, Explore, verification) 모두에 공유:

```python
MODEL = LiteLlm(
    model=os.environ.get("ADK_CC_MODEL", "openai/Qwen3.6-35B-A3B-UD-MLX-4bit"),
    api_base=os.environ.get("ADK_CC_API_BASE", "http://localhost:18000/v1"),
    api_key=os.environ["ADK_CC_API_KEY"],
)
```

모델 id는 타깃 서버가 OpenAI 호환이므로 `openai/` prefix를 사용. 어떤 LiteLLM 지원 백엔드든 env var 오버라이드(`ollama_chat/...`, `anthropic/...` 등)로 동작 — 코드 변경 없음.

## 6.5. Tasks (Stage F — 순수 추적)

리팩토링 후 추적 전용: `command`/`output` 필드 없음, asyncio worker 없음,
`task_stop` 도구 없음. 모델이 명령 실행을 원할 때 `run_bash`를 직접 사용;
task는 단순히 "어떤 작업이 존재하고 어디 서 있는지"의 기록.
세 가지 surface.

**Tools (`tools/task/`).** 네 도구 — `task_create`, `task_get`,
`task_list`, `task_update`. 모두 비파괴적 (상태 변경, 프로젝트
변경 아님), DEFAULT 모드의 permission 엔진 ask-on-destructive flow
트리거하지 않음. Schema는 upstream Claude Code v2의 `Task`
(`src/utils/tasks.ts:76-89`) 미러: `id`, `title`, `description`,
`status` (`pending`/`in_progress`/`completed` 중 하나),
`blocks`/`blocked_by`, `created_at`/`updated_at`, adk-cc 특유의
`tenant_id`/`session_id`.

**Storage (`tasks/storage.py`).** 기본은 `JsonFileTaskStorage`:
task당 하나의 JSON 파일 `<root>/<tenant_id>/<session_id>/<task_id>.json`.
Root는 `~/.adk-cc/tasks/` (`ADK_CC_TASKS_DIR`로 오버라이드). 쓰기는
multi-worker uvicorn 안전을 위해 `filelock.FileLock`을 통해 진행,
event loop를 막지 않도록 `asyncio.to_thread`로 래핑. Upstream의
task별 JSON 레이아웃 미러 (`src/utils/tasks.ts:229`). `InMemoryTaskStorage`는
테스트용으로 남음. `TaskRunner`는 이제 thin storage facade (asyncio
worker pool 없음).

**Reminder injection (`plugins/task_reminder.py`).** Upstream은
활성 task를 나열하는 주기적 `task_reminder` 첨부를 emit
(`src/utils/attachments.ts:3395-3432` + `messages.ts:3680-3699`).
adk-cc는 이를 `TaskReminderPlugin.before_model_callback`으로 포트.
두 조건 모두일 때 발사:

- 마지막 `task_create`/`task_update` 이후 assistant turn 수 ≥
  `ADK_CC_TASK_REMINDER_TURNS_SINCE_WRITE` (기본 10)
- 마지막 reminder 이후 assistant turn 수 ≥
  `ADK_CC_TASK_REMINDER_TURNS_BETWEEN` (기본 10)

트리거되면 디스크에서 활성 task 목록을 읽고
`<system-reminder>` 블록을 `llm_request.config.system_instruction`에 append.
Reminder 텍스트는 도구 이름을 `task_create`/`task_update`로 다시 쓴 채
upstream을 verbatim 미러. Read-only specialist는 skip하고,
`permission_mode == "plan"`일 때도 skip (거기서 task 도구는 필터링되므로
reminder를 보내면 context만 낭비). 마지막 발사는
`state["task_reminder_last_invocation_id"]`에 추적되어 cooldown 카운터가
이후 turn에서 위치 파악 가능.

플러그인은 `agent.py`의 `App.plugins`와 `service/server.py:build_plugins()`
양쪽에 등록됨. 최종 프로덕션 순서:
`[Audit, Tenancy, Permission, Quota, PlanModeReminder, TaskReminder, ToolCallValidator]`.

## 7. Tool-call validator (런타임 안전망)

ADK의 tool-dispatch flow(`google/adk/flows/llm_flows/functions.py:489-504`)는 function_call이 에이전트의 `tools_dict`에 없는 도구를 명명할 때 `ValueError`를 발생시킵니다. 그 에러는 `on_tool_error_callback`을 통해 플러그인에 제공됨; 어떤 플러그인도 개입하지 않으면 ADK가 re-raise하고 run이 중단됨.

`ToolCallValidatorPlugin`이 그 특정 에러에 개입: 잘못된 tool name, 시도된 args, 실제로 사용 가능한 도구, `<system-reminder>` hint를 나열하는 구조화된 `function_response`를 반환. Hint는 "이 에이전트에 없는 도구"와 "plan-mode 정책으로 필터링된 도구"를 구분 — 후자의 경우 무용한 transfer 대신 `exit_plan_mode`를 모델에 가리킴.

동기 부여 실패: prompt drift가 에이전트가 갖지 않은 도구를 모델이 호출하게 만듦 (예: `Explore`에서 `run_bash`, 또는 plan mode에서 `write_file`). 플러그인 없으면 stack trace로 run 중단; 플러그인과 함께면 모델이 corrective tool 결과를 받고 다음 iteration에서 self-correct. 플러그인은 `agent.py`의 `App.plugins`와 `service/server.py:build_plugins()` 양쪽에 등록됨.

## 7.5. Context-length 가드레일

두 계층이 LLM context window 채워짐을 막습니다.

**1차 방어 — ADK의 `EventsCompactionConfig`**(`google/adk/apps/compaction.py`). adk-cc의 `agent.py`는 env(`ADK_CC_COMPACTION_TOKEN_THRESHOLD` + `ADK_CC_COMPACTION_EVENT_RETENTION` for token-threshold mode; `ADK_CC_COMPACTION_INTERVAL` + `ADK_CC_COMPACTION_OVERLAP` for sliding-window)에서 구성하고 `App(events_compaction_config=...)`에 전달. ADK runner가 invocation 후 compaction을 트리거; `LlmEventSummarizer`가 LLM 호출을 처리, function-call/response 페어링을 보존하고 pending call의 compaction을 피하는 안전 split 로직.

전용 compaction 모델은 `ADK_CC_COMPACTION_MODEL` (+ 완전히 별도 provider용 선택적 `_API_BASE`/`_API_KEY`)을 통해 지원됨. Unset 시 ADK가 에이전트의 main 모델로 auto-default.

**안전망 — `ContextGuardPlugin`**(`adk_cc/plugins/context_guard.py`). ADK의 compaction은 반응적 — 성공한 invocation 후에 실행. 단일 turn이 threshold 아래에서 모델 window 초과로 한 step에 점프(예: 하나의 큰 tool 결과)하면 compaction이 반응하기 전에 모델 서버에 500. 플러그인의 `before_model_callback`이 token을 카운트(`litellm.token_counter`, chars/4 fallback)하고:

- **WARN** at `ADK_CC_CONTEXT_WARN_TOKENS` (기본 `ADK_CC_MAX_CONTEXT_TOKENS`의 75%): 관측성을 위한 구조화 로그 라인.
- **REJECT** at `ADK_CC_CONTEXT_REJECT_TOKENS` (기본 95%): 모델 호출 실패 대신 친절한 "context near full" 메시지로 early `LlmResponse` 반환.

플러그인은 항상 attached. `ADK_CC_MAX_CONTEXT_TOKENS` unset 시 no-op — 배포 간 플러그인 체인을 균일하게 유지.

플러그인은 trim하지 않음, summarize하지 않음, LLM을 호출하지 않음. Content-preserving 복구는 ADK의 compaction 소유. 플러그인은 fail-soft만.

## 7.6. 워크스페이스 레이아웃 (per-user / per-session)

두 모양이 존재:

**Dev** (`adk web .`, `default_workspace()`): `ADK_CC_WORKSPACE_ROOT`의 단일 flat 디렉터리 (relative면 CWD 기준 resolve). `WorkspaceRoot.session_scratch_path = None`. 중첩 없음, migration 스토리 없음 — flat이 dev 계약, dev는 정의상 단일 사용자.

**Production** (via `TenancyPlugin` → `TenantContext.workspace()`):

```
<ADK_CC_WORKSPACE_ROOT>/
└── <tenant_id>/
    └── <user_id>/
        ├── (user 파일 — 이 user의 세션 간 persistent)
        ├── .adk-cc/
        │   ├── plans/              ← write_plan 출력
        │   └── tasks/<session>/    ← task JSON 파일
        ├── .cache/                 ← per-user 설치 캐시 (sandbox에 bind-mount)
        └── .sessions/
            └── <session_id>/       ← per-session scratch, 자동 reap
```

`WorkspaceRoot.abs_path = <root>/<tenant>/<user>/` (persistent home; read/write 기본). `WorkspaceRoot.session_scratch_path = <user_home>/.sessions/<session>/` (per-session scratch).

`fs_read_config` / `fs_write_config`이 두 root 아래 경로 모두 허용. `_safe_id`(in `service/tenancy.py`)가 `os.path.join`에 도달하기 전에 tenant_id / user_id / session_id의 path-traversal 거부. 패턴은 `EncryptedFileCredentialProvider._safe_component` 매칭.

**Sandbox 컨테이너** (production): `DockerBackend`가 `<user_home>`을 `/workspace`에 bind-mount. 세션 scratch는 `<user_home>` 아래 중첩이므로 `/workspace/.sessions/<session>/`에 자동으로 보임. 두 번째 bind-mount `<user_home>/.cache` → `/root/.cache`이 per-user 설치 캐시 활성화 (uv / pip 캐시가 user의 세션 간 살아남음). `ADK_CC_DISABLE_INSTALL_CACHE_MOUNT=1`로 비활성.

**라이프사이클**:
- User home: 영원히 지속. Per-user wipe via `rm -rf <root>/<tenant>/<user>/`. 테넌트 offboarding: `rm -rf <root>/<tenant>/`.
- 세션 scratch: `scripts/scratch_reaper.py`가 `ADK_CC_SESSION_SCRATCH_RETENTION_DAYS` (기본 7) 후 reap.
- 설치 캐시: user home과 함께 persist. `rm -rf <root>/<tenant>/<user>/.cache`로 evict.

**Tasks**는 이제 user의 워크스페이스에 anchor (production): `<user_home>/.adk-cc/tasks/<session>/<task>.json`. Dev 경로는 legacy `~/.adk-cc/tasks/<tenant>/<session>/` 유지. `ADK_CC_TASKS_DIR`이 중앙 task 저장소를 원하는 운영자를 위해 두 경로 모두 오버라이드.

**동시성**: same-user parallel 세션은 user home을 공유 — 파일 경쟁 가능. 각 세션은 자체 scratch 보유. v1 입장: 경쟁은 user의 문제 (공유 working tree의 `git`과 같은 모델). COW overlay 또는 세션 lock은 v2로 연기.

## 8. ADK가 우리를 위해 하는 것

ADK 1.31.1에 의존:

- 에이전트 발견과 `adk web` / `adk run` runner.
- `transfer_to_agent` 도구 (sub_agents를 가진 모든 에이전트에 auto-injected).
- Flow 루프, function-call 처리, event 스트리밍, 세션 저장소.
- 비-Gemini 백엔드용 `LiteLlm`.

우리가 **사용하지 않는** 것:

- `AgentTool` — 부모의 stream에서 specialist event를 숨길 것.
- ADK 플러그인 — 구성된 것 없음.
- Workflow agent (`SequentialAgent`, `LoopAgent`, `ParallelAgent`) — 모델 주도 라우팅 원함, 하드코딩 순서 아님.
- `output_schema` / `output_key` — specialist는 free-form 텍스트 보고서 반환.
