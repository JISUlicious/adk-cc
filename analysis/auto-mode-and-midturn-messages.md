# Review: auto mode + sending messages while the agent runs (2026-10-14)

Both features exist in the reference snapshot (`../src`, ≈ v2.1.83–91), so the
upstream designs below are read from code, not inferred from changelogs.

---

## 1. Auto mode — a classifier answers permission prompts

### Upstream design (verified in `src/utils/permissions/`)
- New permission mode `auto` (`permissions.ts:518+`). Order of checks for a tool call:
  1. deny rules still deny; tools that require explicit permission still prompt;
  2. **fast path**: would `acceptEdits` allow it (edits inside the working dir)? → allow, no classifier;
  3. **safe-tool allowlist** (read/grep/glob, task + plan-mode metadata tools) → allow, no classifier (`classifierDecision.ts`);
  4. otherwise → **classifier** (`yoloClassifier.ts`, 1.5k lines).
- The classifier is a separate model call with structured output `{thinking, shouldBlock, reason}`.
  - **Transcript = user text + assistant `tool_use` blocks only.** Assistant prose is excluded on purpose ("model-authored and could be crafted to influence the classifier"); tool results are excluded too. Queued mid-turn user messages are included.
  - **Two stages**: stage 1 answers fast (`max_tokens=64`); only a *block* escalates to stage 2 with chain-of-thought, to cut false positives. Shared system prompt, cached 1h.
  - Rules are three plain-sentence bullet lists the user can override: `allow`, `soft_deny`, `environment`.
- **On block the tool is DENIED back to the model with the reason** — no card. The agent adapts.
- **Denial tracking** (`denialTracking.ts`): 3 consecutive or 20 total blocks → fall back to normal prompting.
- **Classifier unavailable** → fail closed (deny with retry guidance) by default; **transcript too long** → fall back to a prompt.
- **Entering auto strips dangerous allow rules** (`dangerousPatterns.ts`: interpreter/shell/`ssh`/`eval`/`sudo` prefixes like `python *`) and restores them on exit — otherwise an earlier "Allow always" would bypass the classifier.
- Since then upstream added hard deny rules, destructive-git / transcript-tampering / containment-escape rules, and (2.1.277+) moved the classifier server-side.

### adk-cc today
| Piece | State |
|---|---|
| Modes | `default`, `plan`, `acceptEdits`, `bypassPermissions`, `dontAsk` (`permissions/modes.py`) |
| Deterministic command tiers | `classify_command` → `read_only` / `mutating` / `dangerous` / `catastrophic`; read-only auto-allows in every mode, catastrophic hard-denies even under bypass |
| Floors | protected-path floor, out-of-project write floor, skill-script gate (#114, asks even under bypass) |
| Isolation relaxation | #122: isolated sandbox turns `default`/`acceptEdits` into bypass |
| Desktop default | `bypassPermissions` (#83) |
| "Deny back to the model instead of a card" | **already exists** — `dontAsk` converts asks to denials in `engine._decide_impl` |
| Ask → card | three `request_confirmation` sites in `plugins/permissions.py` (~497 general, ~622 skill gate, ~826) + the confirmation-form/resume machinery |
| Model access | `SelectableLlm` + endpoint registry; `chatgpt-codex` has no practical rate limit |

### Requirements to build it
1. **`PermissionMode.AUTO`** in the enum, schema, and both shells' mode control (#125).
2. **Classifier module** (`permissions/auto_classifier.py`): transcript builder from ADK session events (user text + `function_call` parts only — exclude model text and tool results); structured verdict; optional two-stage; dedicated cheap endpoint configurable separately from the session model.
3. **Placement**: in the plugin, where the engine would return ASK under `AUTO`: classify → allow (run) or deny (reuse the `dontAsk` denial path, with the reason). Order preserved: deny rules → catastrophic → protected floors → read-only fast path → classifier.
4. **v1 scope decisions**: `dangerous` tier keeps asking (conservative); skill-script runs (#114) *do* go to the classifier — that gate is the client's main card storm.
5. **Denial tracking** in session state (3 consecutive / 20 total → card).
6. **Fail closed** on classifier error/timeout; fall back to a card when the transcript is too long.
7. **Strip dangerous allow rules on entry** — our broadening can store `python *` / `bash *` style rules.
8. **Rules config**: `allow` / `soft_deny` / `environment` plain-sentence lists, with defaults, per project/user.
9. **Observability**: audit event per decision; tool card shows "auto-approved: <reason>"; classifier cost in the usage view.
10. **Tests**: verdict tests are pure string→decision with a scripted model (no dangerous execution, per house rule); live e2e with benign actions only; **an offline eval first** — replay real tool calls from existing session histories through the classifier and measure false-allow / false-block before shipping.

Cost: one extra model call per non-fast-pathed action (stage 1 ≈ 64 output tokens, ~1–3 s).

---

## 2. Sending a message while the agent is running

### Upstream design (verified in `src/utils/messageQueueManager.ts`, `query.ts`, `messages.ts`)
- Input typed during a turn is **enqueued**, not rejected. Priorities: `now` > `next` (user input) > `later` (task notifications).
- The query loop **drains the queue after tool results are appended** (`query.ts:~1535`: "do this after tool calls are done, because the API will error if we interleave tool_result messages with regular user messages") and feeds the messages to the model as attachments, so **Claude responds within the current turn**.
- Rendering: a user message wrapped in a system reminder; human-typed messages stay visible in the transcript.
- Slash commands are never drained mid-turn (they wait for turn end). Subagents never see user prompts — only the main thread drains them.
- `now` priority **aborts the current operation** (`REPL.tsx:4098`) — that is the "send-now" key (ctrl+enter) added in 2.1.275.
- Queued messages remain editable until drained.

### adk-cc today
- **Turn broker is single-flight**: a second `POST /api/turns` for a busy session raises → 409 (`service/turns.py:365`).
- **The Composer refuses to submit while streaming** (`Composer.tsx:127`) — the only control is Abort.
- 409s are retried with backoff for out-of-band paths (confirmation answers, `/compact`), and `e2e_busy_409_ui` proves a message sent from another path *waits and lands after the turn*.
- Net: **no mid-turn steering**; the user can only abort or wait.

### Key ADK fact that makes it feasible
ADK rebuilds `llm_request.contents` from `invocation_context.session.events` at **every** model step, and user-authored events are included (`flows/llm_flows/contents.py:_get_contents`). `before_model_callback` runs after the request processors — i.e. after the step's tool responses are already in the session — which is exactly upstream's safe drain boundary.

### Requirements to build it
1. **Queue on the broker turn**: `POST /api/turns/{turn_id}/messages` appends to the running turn's queue; if the turn already finished, start a normal new turn instead (race-safe).
2. **Drain in a plugin** at `before_model_callback`, **coordinator only** (a specialist that holds control defers until it hands back): append a user `Event` to `ictx.session` via the session service (persisted + streamed to the UI) *and* append it to the current `llm_request.contents`. Frame it so the model treats it as steering ("The user sent this while you were working: …").
3. **Leftovers at turn end**: a message that arrives during the final model call (no more tool calls) is never seen — the broker must start a follow-up turn with the remaining queue.
4. **Parked confirmation**: while a card is pending, a typed message should not be injected silently; v1 = hold it until the card is answered (or let the message double as "deny + new instruction" — design call).
5. **Slash commands** are never drained mid-turn.
6. **Send-now** (abort + send as a new turn) needs no injection at all — the existing abort plus start; nearly free.
7. **UI**: Composer stays enabled while streaming; queued message chips above the input with edit/cancel; the injected message renders in the thread at the point it was drained; ctrl+enter = send-now.
8. **Interplay**: memory capture and titles already read the whole invocation, so drained messages are captured; the auto-mode classifier transcript must include them.
9. **Durability**: the queue lives in the in-process broker; lost on server restart (acceptable v1, same as running turns).

---

## Recommendation
1. **Send-now first** — abort + resend as a new turn. Hours of work, immediate value.
2. **Mid-turn queue next** — medium effort (broker queue + one plugin + Composer), low security risk, upstream design maps cleanly onto ADK's per-step contents rebuild.
3. **Auto mode as its own track, measured first** — larger and security-sensitive. Start with the offline eval on recorded sessions; ship only if false-allows are near zero on the dangerous-adjacent set. Biggest payoff is web/remote (the #114 confirmation storms); on desktop it could later replace the `bypassPermissions` default with something safer.
