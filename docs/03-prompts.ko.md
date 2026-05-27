# Prompt

## 계보 매핑 (Lineage map)

각 adk-cc prompt는 upstream Claude Code 소스 파일의 충실한 포트(adaptation 포함)입니다. Adaptation은 모든 prompt에서 동일합니다:

- **Tool name rename**: `Read` → `read_file`, `Glob` → `glob_files`, `Grep` → `grep`, `Bash` → `run_bash`, `Edit` → `edit_file`, `Write` → `write_file`.
- **Delegation idiom**: upstream의 `AgentTool` with `subagent_type=X`는 ADK의 `transfer_to_agent(agent_name='X')`이 됨.
- **Hand-off rider**: 각 specialist의 prompt는 "coordinator에게 보고하고, 사용자에게 말 걸지 말 것" 블록으로 끝남. 이것은 adk-cc 특유 — upstream의 `AgentTool`은 sub-agent의 출력을 tool 결과로 반환하므로 사용자 직접 호출 위험이 없었지만, adk-cc의 `sub_agents` 토폴로지는 이를 노출.
- **Plan은 posture, sub-agent 아님**: upstream의 standalone Plan sub-agent에 해당하는 것은 여기 없음. Planning은 coordinator가 취하는 posture (`enter_plan_mode` → `permission_mode = "plan"`); planning instruction은 `PlanModeReminderPlugin.PLAN_MODE_REMINDER`에 있고 `before_model_callback` 시점에 동적으로 주입됨. 텍스트는 upstream의 `getPlanV2SystemPrompt`에서 포트됨.

| Prompt | 위치 | Upstream 소스 |
|---|---|---|
| `EXPLORE_INSTRUCTION` | `adk_cc/prompts.py` | `src/tools/AgentTool/built-in/exploreAgent.ts` (`getExploreSystemPrompt`) |
| `VERIFY_INSTRUCTION` | `adk_cc/prompts.py` | `src/tools/AgentTool/built-in/verificationAgent.ts` (`VERIFICATION_SYSTEM_PROMPT`) |
| `COORDINATOR_INSTRUCTION` | `adk_cc/prompts.py` | `src/constants/prompts.ts`에서 조합 (아래 라인) |
| `PLAN_MODE_REMINDER` | `adk_cc/plugins/plan_mode.py` | `src/tools/AgentTool/built-in/planAgent.ts` (`getPlanV2SystemPrompt`) |

## Prompt별 구조

### `EXPLORE_INSTRUCTION`

섹션: 도입부 → `CRITICAL: READ-ONLY MODE` 금지 목록 → 강점 → 가이드라인 → 속도/병렬 tool-call 노트 → hand-off rider.

READ-ONLY 블록은 구조적 tool denylist에도 불구하고 존재합니다 — prompt 측 강화는 모델이 tool 호출을 발명하지 않게 도와줍니다 (예: `mkdir`에 `run_bash` 사용 시도 — Explore는 `run_bash`가 아예 없음).

### `VERIFY_INSTRUCTION`

섹션: 두 가지 실패 패턴 도입부 (verification 회피, 80%에 유혹됨) → `DO NOT MODIFY THE PROJECT` (`/tmp` 허용 포함) → 타입별 verification 전략 → required baseline → "Recognize your own rationalizations" anti-pattern 목록 → adversarial probe → 출력 형식 → verdict 라인 계약 → hand-off rider.

Verdict 라인은 전체 시스템에서 **유일한 구조적 강제**입니다: verifier의 prompt가 이를 생성하고, coordinator의 prompt가 이를 소비. 나머지는 모두 convention.

### `COORDINATOR_INSTRUCTION`

`src/constants/prompts.ts`의 개별 규칙으로 구성:

| `COORDINATOR_INSTRUCTION`의 섹션 | Upstream 규칙 | 소스 라인 |
|---|---|---|
| HARD RULE preamble (first-action 화이트리스트; `task_create` 우선 금지) | (조합; adk-cc 신규) | — |
| Doing tasks preamble | "primarily request you to perform software engineering tasks" | 222 |
| Read-before-change | "do not propose changes to code you haven't read" | 230 |
| Diagnose-before-switching | "If an approach fails, diagnose why before switching tactics" | 233 |
| Minimum-complexity | "Don't add features, refactor, or introduce abstractions beyond what the task requires" | 201–203 |
| Comments default | "Default to writing no comments. Only add one when the WHY is non-obvious" | 207 |
| Faithful reporting | "Report outcomes faithfully" | 240 |
| GATHER routing | "For broader codebase exploration and deep research, use the AgentTool with subagent_type=Explore" | 378–379 |
| PLAN routing | (조합; `enter_plan_mode` posture 설명) | — |
| ACT routing | (조합; 사소함 — `write_file`/`edit_file`/`run_bash`) | — |
| TRACK routing (ACT-time 체크리스트로서의 task 도구) | upstream task-tool 가이던스, paraphrase | — |
| Executing actions with care | 전체 `getActionsSection()` 블록 | 255–267 |
| VERIFY routing | "Before reporting a task complete, verify it actually works" + verifier-contract 단락 | 211 + 394 |
| Briefing template | (조합; adk-cc 신규) | — |
| Style | "Lead with the answer or action, not the reasoning…" | 412–420 |

HARD RULE preamble은 신규입니다: `task_create`를 first action으로 금지하고 유효한 first action 옵션(read 도구, Explore transfer, `enter_plan_mode`, `ask_user_question`)을 나열합니다. 더 작은 로컬 모델이 GATHER나 PLAN 없이 `task_create`를 먼저 발사하여 아직 이해하지 못한 작업의 task를 늘어놓는 경향이 있었기 때문에 존재합니다.

PLAN routing 규칙은 조합되었습니다 — upstream의 coordinator는 "비사소한 변경 전 항상 plan" 통합 규칙이 없습니다. Upstream의 `Plan` 설명에 암묵적이며, adk-cc는 이를 명시적으로 만들고 sub-agent가 아닌 `enter_plan_mode`(coordinator의 planning posture)로 라우팅합니다.

TRACK routing 규칙은 조합되었습니다 — Stage F 리팩토링 이후 adk-cc의 task 도구는 순수 추적용(실행 의미 없음)이고, 모델은 이들이 acting 중 유지되는 체크리스트이지 GATHER 전에 실행되는 planning surface가 아님을 이해해야 합니다.

Briefing template은 신규입니다: transfer의 brief가 포함해야 할 필드를 명시 (Explore의 depth, verification의 files-changed/approach/plan-path). Upstream은 더 큰 frontier 모델의 판단에 의존하지만, adk-cc는 구조를 명세화합니다.

### `PLAN_MODE_REMINDER`

`prompts.py`가 아니라 `adk_cc/plugins/plan_mode.py`에 있습니다 — agent의 정적 instruction이 아닌 `PlanModeReminderPlugin.before_model_callback`이 동적으로 주입하기 때문입니다. 섹션:

- "YOU ARE CURRENTLY IN PLAN MODE" 헤더 + 금지 (편집 없음, shell 없음, task mutation 없음).
- 4-step process (understand → explore → design → detail), upstream의 `getPlanV2SystemPrompt`에서 포트.
- 필수 출력: Markdown plan으로 `write_plan` (제목 헤딩, problem statement, 4-step body, `### Critical Files for Implementation` 섹션, 선택적 slug for thread identity).
- Turn 종료: `exit_plan_mode`이 승인 게이트; "이게 괜찮나요?" 같은 plain-text 추가 질문 금지.

플러그인은 또한 plan mode 활성 시 `llm_request.tools_dict`와 function-declaration 목록에서 write/exec/task 도구를 필터링하여, prompt가 사용하지 말라고 하는 도구를 모델이 보거나 호출할 수 없게 합니다. Reminder가 근거를 제공하고, 필터가 구조적 강제를 수행합니다.

## Prompt 충실도가 중요한 이유

Coordinator의 prompt는 **routing table**입니다: 각 specialist를 `agent.name`으로 명명합니다. Prompt가 실제 agent 이름(`Explore`, `verification`)에서 drift하면 coordinator가 invalid한 `transfer_to_agent` 호출을 emit하고, ADK의 `TransferToAgentTool` enum 제약이 이를 거부합니다. (`ToolCallValidatorPlugin`이 corrective response로 그것조차 잡지만, 올바른 치료법은 prompt를 정확하게 유지하는 것.)

반대로, verifier의 prompt는 **verdict 생산자**입니다: 리터럴 `VERDICT: PASS|FAIL|PARTIAL` 라인 emit을 멈추면, coordinator의 prompt는 spot-check할 것이 없게 됩니다.

다른 prompt(Explore, plan-mode reminder)는 더 부드럽습니다 — 품질이 떨어질 뿐 루프를 깨지는 않습니다.

## Style adaptation

Upstream의 prompt는 frontier 모델(Claude Sonnet/Opus 4.x)을 가정합니다. adk-cc는 로컬 모델(기본 Qwen 3.6 35B)을 타깃하며 이들은:

- 약한 tool-use 신뢰성 — 따라서 도구가 구조적으로도 거부됨에도 명시적 READ-ONLY MODE 금지 목록; 따라서 `COORDINATOR_INSTRUCTION`의 HARD RULE preamble이 유효한 first action을 화이트리스트.
- 좁은 instruction-following — 따라서 `VERIFY_INSTRUCTION`의 verbose한 adversarial-probe 및 rationalization-recognition 목록.
- 의심 시 사용자에게 말 거는 경향이 더 큼 — 따라서 모든 specialist에 명시적 hand-off rider.

이 adaptation은 보수적 추가이지 제거가 아닙니다: frontier 모델이 adk-cc를 실행해도 여전히 올바르게 동작합니다.
