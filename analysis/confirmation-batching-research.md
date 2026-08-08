# Stacked confirmations: why the same bug keeps coming back

Research note, 2026-08-08, after a third recurrence on the remote server and
two reverted attempts at fixing it (`d4958db`, `1ac44ed` → reverted in
`aca9b7d`).

## The finding that changes everything

**adk-cc already batches confirmation answers.** `ConfirmationFormUiPlugin`
(`plugins/confirmation_form_ui.py`) implements exactly the design I spent an
evening re-inventing one layer up, and its module docstring states it plainly:

> `on_user_message_callback` defers each submission until ALL outstanding
> wraps have been answered, then bundles them into one user event. ADK's
> processor scans that single event and resumes all N tools in one pass.

So the correct mental model is **not** "batching is missing". It is
**"batching exists and is failing"**. Both of my fixes added a *second*
batcher in the Turn Broker on top of the real one, which is why they behaved
so strangely: two systems buffering the same answers, each with its own idea
of when the set was complete.

That also retro-explains the pieces that never fit:

- `adk_cc_pending_confirmation` is not an accident or a leak. It is the
  plugin's **deliberate stash name**, chosen so ADK's processor ignores
  partial answers (`PENDING_CONFIRMATION_NAME`, line 198). The broker's
  `_UNDELIVERED_CONFIRMATION_NAMES` describes the same fact from the outside.
- My broker parking wrote answers to the session under that stash name — i.e.
  it hand-rolled the stash the plugin was already writing, in a shape the
  plugin does not recognise as its own.

## How the real mechanism works

`on_user_message_callback` (line 256):

1. Pick out incoming `function_response` parts named
   `adk_cc_confirmation_form`; everything else passes through untouched.
2. Reshape each to ADK's `ToolConfirmation` shape via `_extract_chose_id`.
3. Read the session and compute three sets:
   - `outstanding = _outstanding_wrap_ids(events)`
   - `already_pending = _stashed_pending_responses(events)`
   - `unresolved = outstanding - _resolved_wrap_ids(events)`
4. **Incomplete** (`not unresolved.issubset(union_ids)`) → re-emit every
   answer renamed to `adk_cc_pending_confirmation` so ADK ignores it. The run
   stays parked.
5. **Complete** → bundle stashed + incoming under the real
   `adk_request_confirmation` name in ONE user event. ADK resumes every tool
   in a single pass, one LLM call follows with all N results.

The design is sound. Step 4/5 is the same rule the broker attempt tried to
enforce: a model turn is legal only when every outstanding call has a
response.

## Where to look, given that

The failure is a **disagreement about set membership** in step 3 — the plugin
concludes the batch is incomplete when the user has answered everything, so
step 5 never fires and the run parks forever. Candidate causes, in the order
I would test them:

0. **`_session_events` fails SILENTLY to an empty list** (line 490): it
   returns `[]` whenever the context, session, or events are missing —
   documented as "best-effort ... tolerant of unusual session shapes". Follow
   the consequence: `outstanding` and `already_pending` both come back empty,
   so `unresolved` is empty, so the guard
   `if not unresolved or not unresolved.issubset(union_ids)` takes the
   **stash** branch. The answer is deferred and the bundle step never runs —
   permanently, with no error anywhere. One unavailable session read parks the
   run forever. This is the cheapest thing to instrument and the most likely
   to explain "just stopped": add a log line on the empty-events path and see
   whether it fires on the remote.

1. **`_session_events(invocation_context)` sees the wrong history.** The
   broker's `install_confirmation_resume_fix` forces confirmation answers into
   a NEW invocation (`_resolve_invocation_id → None`). If the events the
   plugin reads are an invocation-scoped snapshot rather than the full
   session, `already_pending` comes back empty and the batch can never
   complete. **This is the prime suspect**: it is the one thing adk-cc changed
   underneath the plugin, and the recurrences began around that fix.
2. **`_resolved_wrap_ids` vs `allow_always`.** `allow_always` writes a
   permission rule, so the *tool* becomes allowed while the *call* may still
   be unresolved. If a rule write causes a re-raise, `outstanding` grows a new
   wrap id each round — which is exactly the infinite card respawn observed
   when `allow_always` was clicked (never covered by any test).
3. **Durability.** The stash lives in session events. Anything that rewrites
   history — compaction, checkpoint restore of `.adk-cc/**`, orphan pruning
   (`_prune_orphan` deletes the last event) — can drop a stashed answer and
   leave `unresolved` permanently non-empty.

## What NOT to do next

- Do not add batching, buffering, or parking in the broker. That is
  duplicating `ConfirmationFormUiPlugin`. Both attempts made things worse:
  one moved the stall a click later, the other introduced a permission bypass.
- Do not judge a fix by the unit suite. Three rounds shipped green because the
  tests assert on predicates we wrote, never on whether ADK accepts the
  result. `tests/e2e_stacked_confirmations_ui.py` is the only check that has
  ever caught any of this.

## Test matrix any attempt must start with

`allow_once` was the ONLY button ever clicked in the live test, which is how
two `allow_always` failures shipped. From the first run, cover:

| cards | actions |
|---|---|
| 2 | allow_once, allow_once |
| 2 | allow_always, allow_once |
| 2 | allow_always, allow_always |
| 2 | deny, allow_once |
| 1 | each of the three (regression guard) |

`allow_once` and `allow_always` are not variants of one action: the second
MUTATES state. Any design that assumes answers are inert is wrong.

## Current state

`eb132a5` (row keys by `callId`) is kept and is the fix for the reported
symptom — the second card greying out was React reusing a component instance
and leaking the answered card's `submitted` lock. The broker attempts are
reverted. Remaining defect: the plugin's batch can fail to complete, leaving a
round with no reply. Tracked in #115.
