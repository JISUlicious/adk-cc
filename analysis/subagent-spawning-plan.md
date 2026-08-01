# Sub-agent spawning: review of the parked work, and the plan to land it

2026-08-01. The user reopened this. Prior art exists and is good: branch
`feat/agent-tool-parallel-explore` (2 commits, 2026-06-10, 708 added lines
incl. tests), parked by decision on 2026-06-12.

## What the branch already built (reviewed, not recalled)

Today's main has exactly two sub-agents — `Explore` and `verification` — wired
as **transfer** agents: sequential, one active at a time, sharing the parent's
session. The branch adds the other axis: **fan-out**.

* `EnrichedAgentTool` — ADK's `AgentTool` (the model can emit N calls in one
  response; ADK dispatches them concurrently, each in an isolated session)
  with one gap fixed: vanilla `AgentTool` returns a bare string, so N parallel
  results come back unordered and unattributable. The enriched envelope is
  `{task, agent, ok, elapsed_s, queued_s, tool_calls, tools_used, events,
  report, error?}` — attribution, per-explorer cost, and visible failure.
* A process-global semaphore caps concurrent nested runs
  (`ADK_CC_AGENT_TOOL_EXPLORE_MAX`, default 8); excess calls queue and report
  their wait as `queued_s`. The model still chooses the count.
* Two spawnable explorers: `code_explore` (read_file/glob/grep) and
  `web_explore` (web_fetch/grep), both read-only, both carrying an
  iterate-until-sufficient workflow in their tool description: spawn a batch →
  assess coverage → re-spawn only for gaps → synthesize (≤3 rounds typical).
* A hard-won note in the wiring: `skip_summarization` must stay False — with
  it set, the run ends at the tool responses and the coordinator never
  synthesizes or iterates (found via the branch's e2e).
* Opt-in behind `ADK_CC_AGENT_TOOL_EXPLORE=1`; default surface unchanged.
* Unit tests (envelope, cap/queueing) + a live e2e.

**Port cost, measured:** main is 347 commits ahead; `git merge-tree` shows ONE
conflict, and it is trivial (both sides append to `_coordinator_tools` in the
same region). The ADK internals the override mirrors (`_get_input_schema`,
`ForwardingArtifactService`) still exist in the pinned ADK.

## What main grew in the meantime — the actual review findings

The design still holds. Four things built since June change the integration,
and one of them is a safety hole if ported blind:

1. **Permission gates + HITL do not reach into a nested run.** The
   PermissionPlugin now asks (confirmation cards) for protected paths, scope
   exits, dangerous commands, and — since this week — skill scripts. A nested
   `AgentTool` run executes inside one tool call: its events never reach the
   UI, so an "ask" raised inside a child has no one to answer it — the run
   hangs or dies. The branch predates all of these gates. **Children must run
   ask→deny**: mark the child session (`subagent: true`), and the plugin maps
   any ask-decision to a structured deny — "no human is reachable from a
   sub-agent; the coordinator must perform this step itself". Read-only tools
   make this rare; rare is not never (a grep into `.git/config` asks today).
2. **Session-pinned models.** `MODEL` is now a `SelectableLlm` resolving via
   state→contextvar per request. Children are separate sessions; the
   contextvar should propagate into the fan-out's `asyncio.create_task`
   children by contextvars semantics — but that is an assumption about ADK's
   dispatch internals, so it gets a test, not trust.
3. **Durable turns + delete-mid-run (#87).** Aborting a session must cancel
   in-flight children. Cancellation should propagate through the awaited
   gather; again a test, not trust.
4. **Thread legibility (the R5 precedent).** A spawned explorer renders today
   as an anonymous wrench row, and worse, folds into "N tool calls" — the
   exact illegibility skills had. The envelope makes a good row: "Explorer ·
   <task> · ok · 4.1s · 7 tools". Same never-fold rule as skills and plans.

Also noted, deliberately out of scope: **write-capable sub-agents**. Desktop
went in-place partly because "no subagents means worktree isolation earns
nothing" — spawnable read-only explorers do not reopen that, but spawnable
BUILDERS would (parallel writers need the dormant worktree machinery, plus
per-child scope). Separate decision, later.

## Plan

### P0 — port (~half day)
Cherry-pick the two commits onto main behind the same flag; resolve the one
conflict; keep the envelope, cap, and workflow text as-is. Branch unit tests
green. The old branch stays untouched (history), work proceeds on main.

### P1 — the safety adaptation (~half day)
`subagent: true` in the child session state (set by `EnrichedAgentTool` before
the nested run); PermissionPlugin maps ask→deny under it, with a reason the
coordinator can act on. Tests: a protected-path read inside a child returns
the structured deny (not a hang); the same read at the coordinator still asks;
skill-script gate inside a child denies rather than prompts.

### P2 — integration hardening (~half–1 day)
* Model-pinning propagation: child requests carry the session's pinned
  endpoint/model (test against a fake SelectableLlm recording resolutions).
* Abort: delete/abort mid-fan-out cancels children (extend the #87 e2e).
* Thread row for spawned agents (SkillCard pattern; never folded), showing
  task, ok/error, elapsed, tool count from the envelope.

### P3 — live acceptance (~half day)
One multi-question repo-exploration task through the real UI/model:
* fan-out actually parallel (wall-clock < sum of per-explorer elapsed_s),
* results attributable (each report names its task),
* iteration targets gaps (round 2 spawns fewer, different questions),
* the thread shows each explorer as its own row.

### P4 — decide, don't build
Default-on vs opt-in (measure token cost of the two extra tool declarations
first); write-capable builders + worktree isolation — its own plan if wanted.

## Open questions for the user
* Reason it was parked in June — if there was a concern beyond sequencing, it
  should shape P4's default-on decision.
* Should `web_explore` ship too, or `code_explore` first? (Both are in the
  branch; web adds `web_fetch` exposure inside children — ask→deny makes it
  safe, but scope is a choice.)
