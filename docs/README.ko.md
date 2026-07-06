# adk-cc 문서

Scope: 이 디렉터리는 **adk-cc 구현**만을 문서화합니다. Upstream Claude Code의 아키텍처는 직접 관련된 경우(예: prompt의 계보 인용)를 제외하고는 설명하지 않습니다.

- [`01-specification.ko.md`](./01-specification.ko.md) — adk-cc가 무엇인지, 무엇을 하는지, scope 안에 무엇이 있는지. 역할, 동작 계약, 제약, 보류 항목.
- [`02-architecture.ko.md`](./02-architecture.ko.md) — 어떻게 만들어졌는지: 파일 레이아웃, 에이전트 토폴로지, "coordinator owns user I/O"를 강제하는 이중 ADK 메커니즘, plan-mode-as-posture, sandbox 계층, task 추적, 런타임 tool-call validator.
- [`03-prompts.ko.md`](./03-prompts.ko.md) — 에이전트별 prompt 구조와 각각이 포트된 upstream 소스. 동적으로 주입되는 `PLAN_MODE_REMINDER` 포함.
- [`04-deployment-sandbox.ko.md`](./04-deployment-sandbox.ko.md) — Sandbox 운영자 런북: Docker 기반 sandbox 호스트 프로비저닝 (plain TCP 또는 mTLS) **또는** 외부 REST sandbox에 대한 `sandbox_service` 백엔드 stand up (gVisor 격리, 예: [JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing)).
- [`05-production-deployment.ko.md`](./05-production-deployment.ko.md) — End-to-end 배포 런북 + readiness checklist. 토폴로지, 배포 단계, custom auth, 워크스페이스 저장소 티어 (Tier 1 단일 호스트 ~ Tier 4 service-mediated), day-2 ops, alpha-status 갭 목록 (security / reliability / observability / ops / 다중 테넌트 / config / tests).
- [`06-confirmation-protocol.ko.md`](./06-confirmation-protocol.ko.md) — Tool-confirmation HITL prompt의 wire 프로토콜: outbound `ConfirmPrompt` 페이로드 모양, inbound `chose_id` 값, "Allow always" 세션 규칙 스코핑, payload 프로토콜을 말하지 않는 frontend를 위한 legacy `confirmed: bool` fallback.
- [`07-web-ui.ko.md`](./07-web-ui.ko.md) — React 챗 UI 런북: stack, 소스 레이아웃, event flow, long-running tool resume 프로토콜, wire 형식 quirk, slash 명령어, 테마, dev + prod 실행 모드, env 변수.
- [`08-desktop-app.ko.md`](./08-desktop-app.ko.md) — 단일 사용자 데스크톱 앱(Tauri + Python 사이드카): 데이터 디렉터리, `settings.env` 설정 파일, dev 실행 모드, 단일 파일 AppImage 인스톨러 빌드, relocatable 경로 레이아웃.
