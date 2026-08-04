# ChatGPT-subscription model provider (Codex backend) — research + plan

Goal: let adk-cc run inference against a user's **ChatGPT subscription** (Plus/Pro)
via OpenAI's Codex OAuth + backend, the way opencode / pi-mono / Cline do.
**Subscription billing only** — never `api.openai.com`, never an API key, never the
id_token→API-key token-exchange.

## Proven (live test, 2026-07, user's Plus account)

- `POST https://chatgpt.com/backend-api/codex/responses` → 200, real `gpt-5.5` reply.
- Auth: `Authorization: Bearer <access_token>` + `ChatGPT-Account-ID: <account_id>`
  + `originator: codex_cli_rs` + `OpenAI-Beta: responses=experimental`. No API key.
- Subscription-metered proof — response headers:
  `x-codex-plan-type: plus`, `x-codex-active-limit: premium`,
  `x-codex-primary-used-percent` (5h window, `primary-window-minutes: 300`),
  `x-codex-secondary-used-percent` (weekly, 10080), `*-reset-after-seconds`,
  `x-codex-credits-has-credits: False` (no API credits involved).
- Models on this Plus plan: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini` (dynamic, plan-gated;
  `gpt-5.1-codex` → 400 "not supported when using Codex with a ChatGPT account").

## Constants (verified vs openai/codex source)

- Client ID (public): `app_EMoamEEZ73f0CkXaXp7hrann`
- Authorize: `https://auth.openai.com/oauth/authorize`; Token: `.../oauth/token`;
  Revoke: `.../oauth/revoke`
- Redirect: `http://localhost:1455/auth/callback` (fallback 1457) — **fixed** by the
  Hydra allow-list for this client. Only works where browser+store share a machine
  (desktop). Hosted/headless → **device-code** flow
  (`/api/accounts/deviceauth/usercode` → poll `/token`; verify at `/codex/device`).
- Scopes: `openid profile email offline_access api.connectors.read api.connectors.invoke`
- PKCE S256; authorize extra params: `id_token_add_organizations=true`,
  `codex_cli_simplified_flow=true`, `originator=codex_cli_rs`, `state`.
- Token exchange (form): `grant_type=authorization_code, code, redirect_uri, client_id,
  code_verifier` → `{id_token, access_token, refresh_token}`.
- Refresh (JSON): `{client_id, grant_type:"refresh_token", refresh_token}`; proactive
  when access-token `exp` < 5 min (or `last_refresh` > 8 days); reactive on 401; rotate
  the refresh token; permanent-fail codes `refresh_token_{expired,reused,invalidated}`
  → re-login.
- `~/.codex/auth.json`: `{auth_mode:"chatgpt", OPENAI_API_KEY:null,
  tokens:{id_token, access_token, refresh_token, account_id}, last_refresh}`.
  `account_id` = id_token claim `https://api.openai.com/auth`.chatgpt_account_id.

## Inference body (Responses API)

```
{ model, store:false, stream:true, instructions:<system prompt>,
  input:[ ...conversation items, IDs stripped, no item_reference... ],
  include:["reasoning.encrypted_content"],
  reasoning:{effort, summary:"auto"}, text:{verbosity} }
```
No `max_output_tokens`. Terminal SSE: `response.completed|done|incomplete`;
errors via `error`/`response.failed` (`error.code`, `error.plan_type`, `error.resets_at`).

### Gotchas
1. **originator whitelist** — must be `codex_cli_rs` (a third-party brand → 403).
2. **store:false mandatory** → stateless: resend history, strip item IDs, echo
   `reasoning.encrypted_content` for reasoning continuity.
3. **instructions** must be non-empty and Codex-style; own system prompt works for
   general `gpt-5.x` (numman-ali fetches Codex's own prompt for `*-codex` robustness).
4. effort/verbosity validation varies by model; `minimal`→`low`.
5. rate limits: prefer `x-codex-*` headers (live) + `retry-after(-ms)` + `error.code`.

## ToS

Own subscription in your own agent = verbally blessed (Huet tweet; Apache-2.0 fork).
**Pooling / billing other users' plans in a hosted product violates OpenAI's terms.**
→ desktop/single-user is the sanctioned case; multi-tenant is opt-in + warned.

## adk-cc integration points

- `models/selectable.py:_build_litellm` (~:227) — branch on a `chatgpt-codex/…` model
  (or a new `provider` field on `ModelEndpointConfig`, `endpoints.py:24`) to return a
  new `BaseLlm` subclass instead of `LiteLlm`.
- New `models/chatgpt_codex.py` — `ChatGptCodexLlm(BaseLlm)`; direct `httpx` SSE;
  maps `LlmRequest` (system_instruction→instructions, contents→input, tools, effort)
  ↔ Responses API; yields partial+final `LlmResponse` (function_call as
  `types.Part(function_call=...)`, usage in `usage_metadata`).
- Tokens: `credentials/` `CredentialProvider` (Fernet), key `codex_oauth`
  (access/refresh/account_id). Never in session DB / model I/O / logs.
- Registry/routes: reuse `/desktop/settings/models`; add
  `/desktop/settings/codex/{status,import,logout}`; UI section in Settings + picker.
- Multi-tenant (Phase 3): per-user token lookup needs a `before_model_callback`
  stashing the user's token in a contextvar (SelectableLlm gets no user_id).

## Phased plan (decided: desktop-first, import-from-CLI)

- **Phase 1 (MVP, testable now):** `ChatGptCodexLlm` provider + credential storage,
  seeded by "Import from Codex CLI" (copy+own-refresh `~/.codex/auth.json`). Register
  `chatgpt-codex/gpt-5.5`, branch `_build_litellm`, wire the picker. Live e2e through the
  agent (multi-turn + one tool call). Core effort = the LlmRequest↔Responses mapping.
- **Phase 2:** own PKCE browser login (localhost:1455) + "Connect ChatGPT" button;
  live `/codex/models` discovery.
- **Phase 3 (opt-in):** device-code login (hosted), per-user tokens (contextvar),
  plan-usage UI from `x-codex-*`, mapping hardening.
