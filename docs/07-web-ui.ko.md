# Web UI

[`web/`](../web/) 아래에 출하 중인 커스텀 React 챗으로, 사용자용 채팅에서 번들 `adk web` UI를 대체합니다. adk-cc 특유의 동작을 불투명한 tool call이 아닌 first-class affordance로 노출하도록 설계되었습니다.

## Stack

- React 19 + TypeScript + Vite
- Tailwind v4 (`@tailwindcss/vite` 플러그인)
- shadcn/ui primitive (Button, Card, Input) — 손수 설치, Radix 의존성 없음
- Lucide 아이콘
- 상태 라이브러리 없음 — `useState` + 소수의 `useEffect` 훅
- 마크다운 렌더링은 `react-markdown` + `remark-gfm`

빌드된 번들 크기: ~425 KB JS / ~30 KB CSS (gzip ~130 KB / ~7 KB).

## 서버와의 통신

두 가지 endpoint surface, 모두 동일한 FastAPI 프로세스가 서빙 (`adk_cc/service/server.py::build_fastapi_app` 참고):

1. **ADK REST** — `/list-apps`, `/apps/{app}/users/{user}/sessions[/{id}]`, 그리고 SSE turn endpoint `/run_sse`. 커스텀 UI는 번들 `adk web` UI와 *동일한* endpoint를 사용; 에이전트 런타임은 어떤 클라이언트가 말을 거는지 모릅니다.
2. **정적 자산** — `ADK_CC_SERVE_UI=1`일 때 FastAPI 앱이 `web/dist/`를 `/`에 `StaticFiles`와 `/` catch route(`index.html` 반환)로 마운트. Auth middleware가 SPA 경로(`/`, `/favicon.svg`, `/assets/*`)를 면제하여 번들이 익명으로 로드 가능; React 앱이 자체적으로 로그인 폼을 실행.

Auth middleware는 source가 JWT(`JwtAuthExtractor`)이든 정적 dev 맵(`BearerTokenExtractor`)이든 동일한 Bearer 토큰 형식을 수용. UI는 토큰을 `localStorage`(`adk_cc.token`)에 저장하고 모든 API 호출에 `Authorization: Bearer <token>`을 보냄.

## 소스 레이아웃 (`web/src/`)

```
api/
├── client.ts       — Bearer 헤더와 401 토큰 클리어가 있는 typed fetch wrapper
├── auth.ts         — localStorage 토큰 + 사용자 영속화, JWT payload 디코드
├── sessions.ts     — ADK 세션 REST에 대한 typed client (list/create/get/delete/patch)
└── sse.ts          — /run_sse용 POST-fetch SSE 소비자 (text와 function_response newMessage)

components/
├── AuthGate.tsx          — /list-apps preflight 검증이 있는 로그인 폼
├── SessionRail.tsx       — 왼쪽 rail: app picker + session 목록 + new/delete
├── Composer.tsx          — 다중행 textarea, slash 메뉴, plan-mode 뱃지
├── Thread.tsx            — event 평탄화 + row dispatcher
├── MessageBubble.tsx     — 사용자/에이전트 채팅 버블
├── ToolCallCard.tsx      — generic function_call expander (fallback)
├── ToolResponseCard.tsx  — generic function_response expander (fallback)
├── ConfirmationCard.tsx  — permission-engine "ask" prompt
├── AskUserQuestionCard.tsx — 구조화된 다중 선택 폼
├── BashTerminalCard.tsx  — run_bash artifact 렌더러
├── FileEditCard.tsx      — edit_file / write_file diff 렌더러
├── PlanCard.tsx          — write_plan / read_current_plan markdown viewer
├── TaskSidebar.tsx       — task_* event로부터 도출된 오른쪽 rail
├── SettingsDialog.tsx    — 테마 picker + identity + sign-out이 있는 모달
├── SlashCommandMenu.tsx  — 입력이 /로 시작할 때 표시되는 popup picker
└── ui/{button,card,input}.tsx — shadcn primitive

lib/
├── utils.ts        — cn() = twMerge(clsx(...))
└── theme.ts        — light/dark/system 모드, localStorage에 영속화

pages/
└── ChatPage.tsx    — 3-pane 레이아웃 (rail | thread+composer | task sidebar)
```

## Event flow

1. 사용자가 메시지 제출 (textarea Enter 또는 slash-message 선택).
2. `ChatPage.handleSend`가 optimistic 사용자 `RunEvent`를 append하고 `streamRun()`을 호출.
3. `streamRun`이 `streaming: true`로 `/run_sse`에 POST하고 수동 `\n\n` split parser로 SSE 응답을 읽음 (EventSource API는 POST+auth header를 못 함).
4. 각 `data:` JSON 라인이 `RunEvent`로 파싱되어 `events`에 append.
5. `Thread.tsx`:
   - **`dedupePartials`** — ADK는 각 텍스트 chunk를 delta를 운반하는 `partial: true` event로 emit. `(invocationId, author)` 그룹별로 delta를 누적하고, 최종 `partial: false` event가 도착하면(전체 텍스트 + tool call 포함) accumulator를 대체.
   - **`flattenEvents`** — 각 event의 `content.parts`를 walk하고, `thought: true` part(Gemini 내부 thinking)와 whitespace-only text를 필터링하고, 유용한 part(text / functionCall / functionResponse)마다 `ChatRow` emit.
   - **특화 렌더러** — `PAIRED_RENDERERS`의 tool name(run_bash, edit_file, write_file, write_plan, read_current_plan)에 대해서는 call+response가 하나의 paired 카드로 접힘.
   - **인터랙티브 위젯** — `adk_request_confirmation` / `adk_cc_confirmation_form` / `ask_user_question` 호출이 아직 pending(매칭되는 `functionResponse` 없음)일 때, 특화 위젯이 generic ToolCallCard 대신 렌더링.
6. Stream 종료 시, `ChatPage`가 세션을 다시 가져와 canonical event id/timestamp와 `session.state.permission_mode`가 optimistic 상태를 이김, 그리고 `refreshTick`을 증가시켜 session rail이 재정렬되게 함.

## Long-running tool resume

사용자가 `ConfirmationCard`에서 "Allow once"를 클릭하거나 `AskUserQuestionCard`에서 답변을 제출하면 UI가 `streamFunctionResponse()`를 호출. `/run_sse`에 `newMessage`로 POST하는데, 그 `role: "user"` part가 pending 호출의 `id`와 매칭되는 `functionResponse`를 운반. ADK runner가 다음 iteration에서 응답을 pick up하고 에이전트 루프가 계속됨.

이는 번들 `adk web` UI가 사용하는 것과 동일한 프로토콜; 유일한 adk-cc 특유 부분은 응답 모양:

- **Confirmation:** `{ chose_id: "allow_once" | "allow_always" | "deny", comment?, persist_across_sessions? }` — `PermissionPlugin._read_choice_id()`이 `chose_id`에 따라 라우팅.
- **AskUserQuestion:** `{ <question_text>: <chosen_label> | <chosen_label>[] }` — 에이전트 prompt가 이 모양을 기대.

## Wire 형식 quirk

ADK의 `Event` 모델은 `populate_by_name=True`와 함께 `alias_generator=to_camel`을 사용하고, 서버는 `by_alias=True`로 직렬화. 따라서 wire 상의 JSON은 **camelCase** 키 사용 (`functionCall`, `functionResponse`, `invocationId`). TS 인터페이스가 1:1로 매칭. 서버의 Pydantic 모델은 입력에 두 case 모두 수용하므로 outbound function-response 제출은 camelCase든 snake_case든 가능 — 일관성을 위해 camelCase 사용.

`thought: true` part (Gemini "thought summary")는 텍스트가 있는 content part로 도착하지만 절대 채팅 콘텐츠로 렌더링되어선 안 됨. 렌더러가 `flattenEvents`와 `dedupePartials` 양쪽에서 이를 필터링.

## Slash 명령어

UI 전용 sugar — 백엔드 slash 프로토콜 없음. Composer의 첫 글자가 `/`이면 picker가 열림. Tab/Enter가 강조된 옵션 선택; Escape가 닫음.

| Command | Kind | 효과 |
|---|---|---|
| `/help` | action | UI 측 핸들러가 사용 가능한 명령어 목록 user 메시지를 전송 |
| `/clear` | action | `createSession(...)` 후 새 세션으로 전환 |
| `/plan` | action | `PATCH /apps/.../sessions/{id}`에 `state_delta: { permission_mode: "plan" }` — 결정적, LLM round-trip 없음 |
| `/exit-plan` | action | 같은 PATCH 경로, `permission_mode: "default"`로 설정 |
| `/theme` | action | light → dark → system 순환, 영속화 |
| `/settings` | action | `SettingsDialog`를 열음 |
| `/signout` | action | `clearToken()` + reload |

`/plan`과 `/exit-plan`은 ADK의 PATCH endpoint를 통해 세션 상태를 직접 전환하므로 plan 모드는 LLM에 대한 힌트가 아닌 보장된 전환.

## 테마

세 가지 모드 — `light`, `dark`, `system` (기본). `system`은 media-query listener를 통해 OS의 `prefers-color-scheme`을 live로 따름. 선택된 모드는 `localStorage`의 `adk_cc.theme`에 영속화. `initTheme()`이 React가 마운트되기 *전*에 `main.tsx`에서 실행되므로 dark 선호 사용자가 첫 paint에서 light flash를 보지 않음.

## UI 실행

### Production (단일 프로세스)

```bash
npm --prefix web install
npm --prefix web run build

ADK_CC_SERVE_UI=1 \
ADK_CC_AGENTS_DIR=$(pwd) \
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# http://127.0.0.1:8000/ 열고 `devtok`으로 로그인
```

FastAPI 프로세스가 (기존 route 아래의) API와 (`/`의) 정적 SPA 번들을 모두 서빙. `web/dist/`를 다시 빌드해도 서버 재시작 불필요 — 다음 페이지 로드가 새 `index.html`을 pick up.

### Development (HMR)

```bash
# Terminal 1: FastAPI 서버
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# Terminal 2: Vite dev 서버
npm --prefix web run dev
# → http://127.0.0.1:5173 with HMR; /run*, /apps, /list-apps,
#   /api, /admin, /debug을 http://127.0.0.1:8000으로 proxy
```

`ADK_CC_DEV_API=http://other-host:8000`으로 Vite proxy 타깃 오버라이드.

## Env 변수

| Variable | 목적 |
|---|---|
| `ADK_CC_SERVE_UI` | `1`로 설정 시 FastAPI에서 `web/dist/` 마운트 (기본 off) |
| `ADK_CC_UI_DIST` | 번들 경로 오버라이드 (기본: `<repo>/web/dist`) |
| `ADK_CC_DEV_API` | `npm run dev` 동안 Vite proxy 타깃 |

UI는 새로운 auth env 변수를 도입하지 않음 — 나머지 FastAPI 배포와 동일한 `ADK_CC_AUTH_TOKENS` / `ADK_CC_JWT_*` surface 공유.

## 알려진 제한 / phase 4+ 후보

- 아직 SPA route 기반 deep link 없음 (composer/thread가 유일한 route).
- 모바일 전용 레이아웃 없음 — 데스크톱 브라우저에서 동작하지만 3-pane 레이아웃은 ~900 px 미만에서 빠듯.
- OAuth/OIDC 리다이렉트 flow 없음 — 로그인은 Bearer-토큰 paste 폼. JWT는 수용되지만 UI가 `/authorize → /callback` dance를 자체 실행하지 않음.
- 번들이 아직 code-split되지 않음.
