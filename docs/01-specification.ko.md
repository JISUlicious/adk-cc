# 명세 (Specification)

## 목적

`adk-cc`는 Google ADK 1.31.1 위에서 동작하는 **coordinator + specialist** 에이전트 루프로, Claude Code의 gather → plan → act → verify 규율을 모델로 합니다. 개발 시에는 `adk web` 또는 `adk run`으로 로드 가능한 단일 에이전트 모듈로 동작하고, 다중 테넌트 배포 시에는 `adk_cc.service.server:make_app`을 통한 FastAPI 서비스로 동작합니다.

## Surface

- **Discovery**: `adk web` / `adk run`은 모듈 레벨 export인 `adk_cc.agent.app`(권장) 또는 `adk_cc.agent.root_agent`로 에이전트를 찾습니다. 디렉터리 레이아웃은 ADK의 문서화된 관례를 따릅니다: `<AGENTS_DIR>/<agent_name>/{__init__.py, agent.py}`.
- **Entry points**:
  - 개발: `adk-cc/` 디렉터리에서 `adk web .` 또는 `adk run adk_cc`.
  - 단일 인스턴스 / 다중 테넌트 서버: `uvicorn adk_cc.service.server:make_app --factory` (auth middleware, quota, 설정 가능한 session storage를 갖춘 FastAPI 앱). 동일한 팩토리가 단일 테넌트 배포(팀 하나, 정적 토큰)와 다중 테넌트 프로덕션(JWT 인증, per-tenant 자원) 모두를 처리합니다 — 차이는 어떤 auth + per-tenant env 변수를 와이어링하느냐 뿐입니다.
- **Configuration**: env 기반. 개발에서는 `adk_cc/.env`에서 자동 로드됩니다. 최소 요구사항: `ADK_CC_API_KEY`. 전체 surface는 [`.env.example`](../.env.example)에 문서화되어 있습니다 — model, permission, sandbox backend, audit, web fetch, skills, tasks, 다중 테넌트 service 변수.

## 역할 (Roles)

세 명의 에이전트:

| Agent | 역할 | Tools |
|---|---|---|
| `coordinator` (root) | 사용자 I/O 소유. 다음 단계가 무엇인지 결정. 간단한 작업은 직접 처리, 복잡한 작업은 위임. `enter_plan_mode`를 호출하면 planning 에이전트가 됨 (tool surface 좁아짐, planning instruction 주입). | 모든 read 도구, write/exec 도구, task 도구, plan-mode 도구(`enter_plan_mode`, `exit_plan_mode`, `write_plan`, `read_current_plan`), `ask_user_question`, `web_fetch`, 그리고 자동 로드된 skill 및 MCP toolset |
| `Explore` | 읽기 전용 코드베이스 탐색자. 작성된 보고서 반환. | `read_file`, `glob_files`, `grep`, `web_fetch` |
| `verification` | Adversarial 검증자. 빌드/테스트/probe 실행. 파싱 가능한 `VERDICT: PASS\|FAIL\|PARTIAL` 라인으로 종료. | `read_file`, `glob_files`, `grep`, `run_bash`, `web_fetch`, `read_current_plan` (prompt로 `/tmp` 외 쓰기 금지 강제) |

Planning은 sub-agent가 **아닙니다**. coordinator가 plan mode로 들어가 직접 처리합니다 ([02-architecture.md §3.5](./02-architecture.md#35-plan-mode-as-coordinator-posture) 참고).

## 동작 계약 (Behavior contract)

**사용자 대면 I/O는 coordinator의 소유입니다.** Specialist는 절대로 사용자에게 말을 걸지 않습니다 — 그들의 보고서는 event stream(과 `adk web` UI)에 투명성을 위해 보이지만, 최종 사용자 대면 응답은 항상 coordinator에서 나옵니다. 이는 두 가지 ADK 메커니즘으로 강제됩니다 ([02-architecture.md §3](./02-architecture.md#3-coordinator-owns-user-io-dual-mechanism) 참고).

**Gather → plan → act → verify는 state machine이 아닌 규율입니다.** 런타임이 단계를 순서화하지 않으며, coordinator가 turn마다 다음이 무엇인지 결정합니다. 순서는 coordinator의 prompt가 유도합니다:

- **Gather**: `read_file` / `glob_files` / `grep`을 통한 directed lookup; `transfer_to_agent(agent_name='Explore')`로 넓은 탐색.
- **Plan**: 작업에 사용자 승인을 동반한 written plan이 필요할 때 coordinator가 `enter_plan_mode`를 호출. Plan mode 안에서 `write_plan`으로 plan을 작성하고 `exit_plan_mode`로 종료. 사소한 작업은 건너뜀.
- **Act**: `write_file`, `edit_file`, `run_bash`. 위험한 작업(파괴적 op, force-push, 공유 상태 변경)은 prompt에 따라 사용자 확인 필요.
- **Track**: 다단계 작업의 ACT-time 진행 가시성을 위한 `task_create` / `task_update` / `task_list` / `task_get`. Plan mode에서는 필터링됨.
- **Verify**: 비사소한 구현(3+ 파일 편집, backend/API, 인프라)에 필수. Coordinator가 게이트 소유; verifier의 verdict가 파싱되고 coordinator는 spot-check 필수.

## 제약 (Constraints)

- **Specialist는 프로젝트를 mutate할 수 없습니다.** `write_file`, `edit_file`, 또는 전체 `run_bash`이 없습니다 (verification은 `run_bash`을 갖지만 prompt가 쓰기를 `/tmp`로 제한). Specialist에게 해당 도구를 주지 않는 것으로 강제.
- **Specialist는 재귀하거나 옆으로 이동할 수 없습니다.** 각각이 `disallow_transfer_to_peers=True`를 가짐; 어느 누구도 tool surface에 `AgentTool`을 나열하지 않음.
- **Specialist는 향후 사용자 메시지의 active agent가 될 수 없습니다.** 각각이 `disallow_transfer_to_parent=True`를 가짐; 결과적으로 ADK runner는 다음 turn을 coordinator로 라우팅합니다 ([02-architecture.md §3.1](./02-architecture.md#31-cross-turn-disallow_transfer_to_parenttrue) 참고).
- **Verification verdict는 파싱된 계약입니다.** Verifier의 prompt가 리터럴 `VERDICT: PASS|FAIL|PARTIAL` 라인을 요구; coordinator의 prompt가 그 라인에 따라 행동하고 PASS에서 spot-check 필수.
- **Plan-mode tool surface는 LLM 계층에서 필터링됩니다.** `PlanModeReminderPlugin.before_model_callback`이 `permission_mode == "plan"`일 때 `llm_request.tools_dict`와 function-declaration list에서 write/exec/task 도구를 제거. 모델은 보이지 않는 것을 호출할 수 없음; `ToolCallValidatorPlugin`이 안전망으로 환각된 호출을 잡음.

## Scope 밖 (보류 또는 pluggable, 구현되지 않음)

- 커스텀 CLI 또는 web UI (개발용 `adk web` / `adk run`; prod용 `uvicorn ... make_app --factory`). **2026-05 업데이트**: 커스텀 React 챗 UI가 [`web/`](../web/)에서 출하 중입니다 — 자세한 내용은 [07-web-ui.ko.md](./07-web-ui.ko.md) 참고.
- E2B / Kubernetes / Modal / nsjail sandbox backend. 오늘 구현됨: `NoopBackend` (호스트 실행, 개발용), `DockerBackend` (per-session container), `SandboxServiceBackend` (외부 gVisor 격리 sandbox service에 대한 REST client, 예: [JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing)). `E2BBackend`는 스텁. `SandboxBackend` ABC가 seam; 새 backend는 `make_default_backend()` 상류에 변경 없이 끼워짐.
- `run_bash` 내부의 per-host outbound 네트워크 필터링 (오늘: 전부 또는 무; per-domain 필터링은 사이드카 프록시 필요).
- 합성 `_handback_to_coordinator` 호출에 대한 실제 `transfer_to_agent` 핸들러 (제어 신호일 뿐 — [02-architecture.md §3.2](./02-architecture.md#32-same-turn-after_agent_callback) 참고).
- [05-production-deployment.md](./05-production-deployment.md)에서 추적되는 프로덕션 readiness 갭 — health endpoint, Prometheus 메트릭, Helm 차트, container reaper, LLM cost ceiling, storage quota, tenant lifecycle, CI / regression suite. 운영자는 위협 모델과 SLO에 따라 필요한 항목을 닫음.
