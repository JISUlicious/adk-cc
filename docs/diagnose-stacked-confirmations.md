# Stacked confirmations: client-run diagnosis + fix

Symptom: two `run_skill_script` confirmation cards; allowing the first
freezes/fades the second. Two DIFFERENT causes share this face — this runbook
splits them with the instrumentation already deployed. Background:
`analysis/confirmation-batching-research.md`.

Run everything on the machine that serves the UI, in the repo root.

## Step 1 — Is the frontend bundle current? (most likely cause)

The unclickable-card fix (`eb132a5`, React rows keyed by callId) is
CLIENT-SIDE JS. `git pull` + server restart deploys none of it.

```bash
grep -l "pair:" web/dist/assets/*.js web/dist-desktop/assets/*.js 2>/dev/null
ls -lt web/dist*/assets/*.js 2>/dev/null | head -3
```

| result | verdict | action |
|---|---|---|
| no `pair:` match, or bundle older than the pull | stale bundle — expected symptom | `cd web && npm run build && npm run build:desktop`, restart, HARD-refresh the browser (cache-bust), retest |
| `pair:` present and fresh | UI fix is deployed | go to Step 2 |

After a rebuild, retest the scenario before continuing: in many reports this
alone closes it.

## Step 2 — What did the batcher decide?

The plugin logs every stash/bundle decision and every blind spot:

```bash
grep "confirmation batch:" server.log | tail -30
```

Reproduce the two-card scenario once, then read the lines nearest the clicks:

| observed | meaning | action |
|---|---|---|
| after click 1: `... -> STASH` with the OTHER wrap id in `unresolved` | CORRECT — deferring until the batch completes | click the second card; expect `-> BUNDLE` and both scripts to run |
| after click 2: `-> BUNDLE` but nothing runs | delivery failure past the plugin | capture Step 3 and report |
| after click 2: still `-> STASH` | set-membership bug caught in the act | the printed sets name the id it wrongly thinks is missing — report the lines verbatim |
| `outstanding=[]` on a click that clearly had cards | signature/scan failure | report lines |
| `NO events` / `NO session` / `NO invocation context` warnings | suspect #0 — the plugin read empty history and deferred blind | report lines + how the session was created (fresh? resumed? after restart?) |
| ZERO `confirmation batch:` lines at all | server predates the instrumentation | `git log --oneline -1` and redeploy; everything after commit `0fca05f` has it |

## Step 3 — Capture for the fix (only if Step 2 shows a server fault)

```bash
grep -E "confirmation batch:|session_title:|resolved roots:" server.log | tail -40
```

Plus the session's event tail (from the UI's session, or):

```bash
# <data-root> is printed in the "resolved roots:" boot banner
tail -5 <data-root>/projects/<user>/sessions/<session>.jsonl | cut -c1-400
```

Report: the grep output, which buttons were clicked (allow_once vs
allow_always — say which; they fail differently), and whether the second card
was greyed out or clickable-but-ignored.

## Expected end states

- Stale bundle (Step 1): rebuild fixes it outright. Most likely.
- Correct STASH→BUNDLE flow: after the LAST click both scripts run and the
  model replies. NOTE: after the FIRST click, no reply and no visible
  progress is CORRECT — the run resumes only when the batch completes.
- Anything else: the captured lines are sufficient to build the fix against —
  the sets in the log are the plugin's actual beliefs, not a reconstruction.

## Known limits (do not chase these as bugs)

- `allow_always` on stacked cards is under-tested territory (see the research
  note's matrix); if the failure only happens with allow_always, SAY SO in
  the report — it changes the fix.
- An answered card stays enabled until the batch completes; re-clicking is
  harmless.
