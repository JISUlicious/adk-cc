# Web UI

A custom React chat shipping under [`web/`](../web/) that replaces the bundled `adk web` UI for end-user chat. Designed to surface adk-cc-specific behaviors as first-class affordances instead of opaque tool calls.

## Stack

- React 19 + TypeScript + Vite
- Tailwind v4 (`@tailwindcss/vite` plugin)
- shadcn/ui primitives (Button, Card, Input) — hand-installed, no Radix dependency
- Lucide icons
- No state library — `useState` + a small handful of `useEffect` hooks

Built bundle size: ~270 KB JS / ~30 KB CSS (gzip ~83 KB / ~6 KB).

## How it talks to the server

Two endpoint surfaces, both served by the same FastAPI process (see `adk_cc/service/server.py::build_fastapi_app`):

1. **ADK REST** — `/list-apps`, `/apps/{app}/users/{user}/sessions[/{id}]`, plus the SSE turn endpoint `/run_sse`. The custom UI uses the *same* endpoints as the bundled `adk web` UI; the agent runtime doesn't know which client is talking to it.
2. **Static assets** — when `ADK_CC_SERVE_UI=1`, the FastAPI app mounts `web/dist/` at `/` via `StaticFiles` and a `/` catch route that returns `index.html`. The auth middleware exempts the SPA paths (`/`, `/favicon.svg`, `/assets/*`) so the bundle can load anonymously; the React app then runs the login form itself.

The auth middleware accepts the same Bearer token format whether the source is JWT (`JwtAuthExtractor`) or a static dev map (`BearerTokenExtractor`). The UI stores the token in `localStorage` (`adk_cc.token`) and sends `Authorization: Bearer <token>` on every API call.

## Source layout (`web/src/`)

```
api/
├── client.ts       — typed fetch wrapper with Bearer header, 401 token-clear
├── auth.ts         — localStorage token + user persistence, JWT payload decode
├── sessions.ts     — typed client for ADK's session REST (list/create/get/delete/patch)
└── sse.ts          — POST-fetch SSE consumer for /run_sse (text and function_response newMessages)

components/
├── AuthGate.tsx          — login form with /list-apps preflight verification
├── SessionRail.tsx       — left rail: app picker + session list + new/delete
├── Composer.tsx          — multi-line textarea, slash menu, plan-mode badge
├── Thread.tsx            — event flattener + row dispatcher
├── MessageBubble.tsx     — user/agent chat bubble
├── ToolCallCard.tsx      — generic function_call expander (fallback)
├── ToolResponseCard.tsx  — generic function_response expander (fallback)
├── ConfirmationCard.tsx  — permission-engine "ask" prompt
├── AskUserQuestionCard.tsx — structured multi-choice form
├── BashTerminalCard.tsx  — run_bash artifact renderer
├── FileEditCard.tsx      — edit_file / write_file diff renderer
├── PlanCard.tsx          — write_plan / read_current_plan markdown viewer
├── TaskSidebar.tsx       — right rail derived from task_* events
├── SettingsDialog.tsx    — modal with theme picker + identity + sign-out
├── SlashCommandMenu.tsx  — popup picker shown when input starts with /
└── ui/{button,card,input}.tsx — shadcn primitives

lib/
├── utils.ts        — cn() = twMerge(clsx(...))
└── theme.ts        — light/dark/system mode, persisted in localStorage

pages/
└── ChatPage.tsx    — 3-pane layout (rail | thread+composer | task sidebar)
```

## Event flow

1. User submits a message (textarea Enter or slash-message pick).
2. `ChatPage.handleSend` appends an optimistic user `RunEvent` and calls `streamRun()`.
3. `streamRun` POSTs `/run_sse` with `streaming: true` and reads the SSE response with a manual `\n\n`-split parser (the EventSource API can't do POST+auth headers).
4. Each `data:` JSON line is parsed into a `RunEvent` and appended to `events`.
5. `Thread.tsx`:
   - **`dedupePartials`** — ADK emits each text chunk as a `partial: true` event carrying a delta. We accumulate deltas per `(invocationId, author)` group; when the final `partial: false` event arrives (which contains the full text + any tool calls), it replaces the accumulator.
   - **`flattenEvents`** — walks each event's `content.parts`, filters out `thought: true` parts (Gemini internal thinking) and whitespace-only text, and emits a `ChatRow` per useful part (text / functionCall / functionResponse).
   - **Specialized renderers** — for tool names in `PAIRED_RENDERERS` (run_bash, edit_file, write_file, write_plan, read_current_plan), the call+response collapse into one paired card.
   - **Interactive widgets** — for `adk_request_confirmation` / `adk_cc_confirmation_form` / `ask_user_question` calls, while still pending (no matching `functionResponse`), the specialized widget renders instead of the generic ToolCallCard.
6. On stream close, `ChatPage` re-fetches the session so canonical event ids/timestamps and `session.state.permission_mode` win over optimistic state, and bumps a `refreshTick` so the session rail re-orders.

## Long-running tool resume

When the user clicks "Allow once" in `ConfirmationCard` or submits answers in `AskUserQuestionCard`, the UI calls `streamFunctionResponse()`. That POSTs `/run_sse` with a `newMessage` whose `role: "user"` part carries a `functionResponse` matching the pending call's `id`. ADK's runner picks up the response on the next iteration and the agent loop continues.

This is the same protocol the bundled `adk web` UI uses; the only adk-cc-specific piece is the response shape:

- **Confirmation:** `{ chose_id: "allow_once" | "allow_always" | "deny", comment?, persist_across_sessions? }` — `PermissionPlugin._read_choice_id()` routes on `chose_id`.
- **AskUserQuestion:** `{ <question_text>: <chosen_label> | <chosen_label>[] }` — the agent prompt expects this shape.

## Wire format quirks

ADK's `Event` model uses `alias_generator=to_camel` with `populate_by_name=True`, and the server serializes with `by_alias=True`. So the JSON over the wire uses **camelCase** keys (`functionCall`, `functionResponse`, `invocationId`). The TS interfaces match that 1:1. The server's Pydantic models accept either case on input, so outbound function-response submits can be camelCase or snake_case — we use camelCase for consistency.

`thought: true` parts (Gemini "thought summaries") arrive as content parts with text but should never render as chat content. The renderers filter them in both `flattenEvents` and `dedupePartials`.

## Slash commands

UI-only sugar — no backend slash protocol. The picker opens when the composer's first character is `/`. Tab/Enter picks the highlighted option; Escape closes.

| Command | Kind | Effect |
|---|---|---|
| `/help` | action | UI-side handler sends a user message listing the available commands |
| `/clear` | action | `createSession(...)` and switches to the new one |
| `/plan` | action | `PATCH /apps/.../sessions/{id}` with `state_delta: { permission_mode: "plan" }` — deterministic, no LLM round-trip |
| `/exit-plan` | action | Same PATCH route, sets `permission_mode: "default"` |
| `/theme` | action | Cycles light → dark → system, persisted |
| `/settings` | action | Opens `SettingsDialog` |
| `/signout` | action | `clearToken()` + reload |

`/plan` and `/exit-plan` flip session state directly via ADK's PATCH endpoint so plan mode is a guaranteed transition rather than a hint to the LLM.

## Theme

Three modes — `light`, `dark`, `system` (default). `system` follows the OS `prefers-color-scheme` live via a media-query listener. The chosen mode is persisted to `localStorage` under `adk_cc.theme`. `initTheme()` runs in `main.tsx` *before* React mounts so dark-preferring users don't get a light flash on first paint.

## Running the UI

### Production (one process)

```bash
npm --prefix web install
npm --prefix web run build

ADK_CC_SERVE_UI=1 \
ADK_CC_AGENTS_DIR=$(pwd) \
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# open http://127.0.0.1:8000/ and sign in with `devtok`
```

The FastAPI process serves both the API (under its existing routes) and the static SPA bundle at `/`. Rebuilding `web/dist/` doesn't require a server restart — the next page load picks up the new `index.html`.

### Development (HMR)

```bash
# Terminal 1: FastAPI server
ADK_CC_AUTH_TOKENS='devtok=alice:acme' \
.venv/bin/uvicorn adk_cc.service.server:make_app --factory \
  --host 127.0.0.1 --port 8000

# Terminal 2: Vite dev server
npm --prefix web run dev
# → http://127.0.0.1:5173 with HMR; proxies /run*, /apps, /list-apps,
#   /api, /admin, /debug to http://127.0.0.1:8000
```

Set `ADK_CC_DEV_API=http://other-host:8000` to point the Vite proxy elsewhere.

## Env knobs

| Variable | Purpose |
|---|---|
| `ADK_CC_SERVE_UI` | `1` to mount `web/dist/` from FastAPI (off by default) |
| `ADK_CC_UI_DIST` | Override the bundle path (default: `<repo>/web/dist`) |
| `ADK_CC_DEV_API` | Vite proxy target during `npm run dev` |

The UI doesn't introduce new auth env vars — it shares the same `ADK_CC_AUTH_TOKENS` / `ADK_CC_JWT_*` surface as the rest of the FastAPI deployment.

## Known limitations / phase 4+ candidates

- No SPA route-based deep links yet (the composer/thread is the only route).
- Plan cards render markdown as a styled `<pre>`; phase 4+ swaps in a real markdown renderer.
- No OAuth/OIDC redirect flow — login is a paste-the-Bearer-token form. JWTs are accepted but the UI doesn't run the `/authorize → /callback` dance itself.
- No mobile-specific layout — works in a desktop browser but the 3-pane layout is tight under ~900 px.
- Bundle is not yet code-split.
