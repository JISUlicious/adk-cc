# Gap Analysis — Workspace & Execution Model

adk-cc vs **Claude Code** (leak `src/` + official v2.1.200, Jul 2026) vs **Hermes**
(`NousResearch/hermes-agent`). Triggered by the desktop question: *shouldn't the
project root be the workspace?* The answer from both peers is **yes**.

## TL;DR

adk-cc is the **outlier** on the axis that matters for local desktop use:

| | Default workspace | Undo / safety net | Stronger isolation |
|---|---|---|---|
| **Claude Code** | **in-place** in cwd/project root | `/rewind` file checkpoints (`.claude/projects/<sess>/file-checkpoints/`) | opt-in git **worktree** (`--worktree`, + subagent `isolation: worktree`) |
| **Hermes** | **in-place** in host cwd (`runtime_cwd.py`) | transparent **shadow-git checkpoints** (`~/.hermes/checkpoints/`) | opt-in **exec backends** (docker/singularity bind-mount, ssh/modal/daytona remote) |
| **adk-cc (desktop)** | **isolated git worktree per session** (always) | **none** | worktree *is* the isolation; sandbox = `noop` (no OS guard) |

Both peers **work on your real files by default** and make that safe with a
**transparent snapshot/rewind** layer + **opt-in** heavier isolation. adk-cc did
the opposite: it made worktree isolation the *default for every session*, and
shipped **neither** of the two things that make in-place safe (rewind, OS
sandbox). That's the gap you felt.

## 1. Workspace / execution — detail

**Claude Code.** `bootstrap/state.ts` tracks *current + original working
directory + project root*; `Edit`/`Write`/`Bash` hit the real files. Safety:
(a) per-edit checkpoints with `/rewind` (session-local, file-only, separate from
git); (b) an **OS-level Bash sandbox** (macOS Seatbelt / Linux bubblewrap) with
filesystem allow/deny, per-domain network prompts, and **credential masking**
(`sandbox.credentials`). Git **worktrees** are *opt-in* (`--worktree` →
`.claude/worktrees/<name>/`, base `origin/HEAD` or `head`, auto-cleanup) and for
**subagents** (`isolation: worktree`) — not the main session.

**Hermes.** `agent/runtime_cwd.py` resolves cwd from the launch dir (or a pinned
per-session cwd); local backend runs in-place on the host. Isolation is a
**pluggable backend** knob (`tools/environments/`): `local` (in-place),
`docker`/`singularity` (hardened container but **bind-mount** = live host FS),
`ssh`/`modal`/`daytona` (genuinely remote, synced via `FileSyncManager`). Undo:
a **shared shadow git store** (`~/.hermes/checkpoints/`) auto-snapshots the
working dir before any mutating op, once per turn, rollback supported — invisible
to the model. Permissions: `tools/approval.py` (pattern detection +
**LLM smart-approval** + persisted allowlist + injection-hardened "YOLO"),
`path_security.py` (`resolve()`+`relative_to()`, reject `..`).

**adk-cc.** `desktop_workspace.py::workspace()` **always** calls
`ensure_worktree()` when a repo is bound → `~/.adk-cc-desktop/worktrees/
<project>/<session>` on branch `adk-cc/<session>`. Desktop sandbox = `noop`
(local exec, no filesystem/network restriction). No checkpoint/rewind. The
agent's edits live on a hidden branch you must reconcile.

## 2. Where adk-cc lags (gaps)

- **P0 — Workspace default.** Every session in a worktree is *subagent-grade*
  isolation applied to the *main* flow. Both peers: in-place default. For a
  single-user local desktop app this is the primary UX gap.
- **P0 — No rewind / checkpoints.** CC (`/rewind`) and Hermes (shadow-git store)
  both give transparent file-snapshot undo. adk-cc has nothing — worktree
  branches aren't an undo UX.
- **P1 — No OS sandbox on desktop.** `noop` = the agent can touch anything.
  CC/Hermes offer real filesystem+network isolation (Seatbelt/bubblewrap /
  containers) with **credential masking**. adk-cc *has* a sandbox abstraction
  (daytona backend) but desktop opts out of isolation entirely.
- **P1 — Permission engine is thinner.** adk-cc has allow/deny/ask + modes +
  a read-only-bash classifier (good). Missing vs CC/Hermes: **classifier /
  LLM-driven auto-approve**, richer rule sources, and **workspace-trust** gating.
- **P2 — No user-facing hooks.** CC has 30+ lifecycle hooks (PreToolUse,
  PostToolUse, WorktreeCreate, SubagentStop, …); Hermes has post-turn nudges.
  adk-cc's ADK plugin callbacks are dev-only, not a user extension surface.
- **P2 — Subagents.** CC runs subagents **background-by-default** with
  **auto-commit/push/draft-PR**; Hermes has parallel batch delegation. adk-cc's
  Explore/verification are synchronous, foreground.
- **P3 — Tool breadth.** Missing LSP, browser automation, PowerShell, cron /
  proactive / remote-trigger (some deferred in adk-cc).

## 3. Where adk-cc *leads*

- **Multi-tenant web service.** Neither CC (single-user local) nor Hermes
  (single-user, multi-channel) has a real tenancy layer. adk-cc: auth +
  `ADK_CC_TENANCY_MODE=single|multi` + **per-user secrets / MCP / skills** +
  the tenant∪user union model. This is adk-cc's genuine differentiator.
- **Deployment surface.** adk-cc ships as a hosted web service *and* a desktop
  app (Tauri + Python sidecar + `settings.env` + self-contained AppImage). CC is
  a terminal/IDE product; Hermes a VPS/TUI + gateway.
- **Plan-mode-as-posture** with read-only-shell — matches CC's plan mode.

## 4. Convergences (validation of recent adk-cc work)

- **`ask_user_question` must pause** — official CC **v2.1.200 (Jul 3 2026)**
  shipped *"AskUserQuestion dialogs no longer auto-continue"* — the exact bug
  fixed here this session.
- **Read-only bash in plan mode** — matches CC plan mode (reads + read-only
  commands allowed, writes blocked).
- **Daytona sandbox backend** — Hermes ships a `daytona.py` backend too;
  independent convergence on the same remote-sandbox provider.
- **Worktrees** — the right tool, but both peers scope them to *opt-in parallel
  work / subagents*, not the per-session default.

## 5. Recommendation (prioritized)

1. **P0 — Make desktop in-place by default.** `workspace()` should return the
   project `repo_path` directly for the desktop/local path; keep worktrees as an
   *opt-in* ("isolate this session") and for parallel subagents — i.e. fork by
   code path (in-place local, isolation for multi-tenant/prod), matching the
   existing "simple for local, isolation for production" principle. This is what
   both CC and Hermes do and what you expected.
2. **P0/P1 — Add checkpoint/rewind.** The prerequisite that makes in-place safe.
   Cheapest: a Hermes-style shadow-git store (snapshot the workspace before
   mutating tools, once per turn) or CC-style per-edit file snapshots + a
   `/rewind`. Without this, in-place is riskier than the worktree it replaces.
3. **P1 — Real desktop sandbox.** Replace `noop`'s "no isolation" with an
   OS-level bash sandbox (Seatbelt/bubblewrap) or at least filesystem/network
   allow-deny + credential masking, reusing the existing sandbox abstraction.
4. **P2 — User-facing hooks** (expose the plugin lifecycle) and
   **classifier auto-approve** for permissions.
5. **P2 — Background subagents** (+ optional auto-PR) for long parallel work.

The through-line: adk-cc's **web-service/tenancy** side is ahead of both peers;
its **local single-user execution model** is behind both. Closing #1 + #2
brings the desktop app in line with how Claude Code and Hermes actually work.
