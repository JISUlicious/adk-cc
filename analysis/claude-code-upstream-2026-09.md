# Claude Code upstream: what changed since our source snapshot (as of 2026-09-09)

**Our reference source** (`../src`, the March 31 2026 sourcemap leak) is
≈ v2.1.83–2.1.91: it references `ultrareview` and the `2.1.83` literal, and
predates the Monitor tool (v2.1.92). **Latest release: v2.1.266 (2026-09-08).**

**Newer source is NOT obtainable.** Since v2.1.105 (Week 16, April) the CLI
ships as a native binary; the npm tarball is now 7 files / 183 kB
(`cli-wrapper.cjs`, `install.cjs`, `sdk-tools.d.ts`) — no JS bundle, no
sourcemap. Everything below comes from the public changelog and the weekly
"What's new" digests, not code.

## Features added after our snapshot (newest first)

| When | Feature | Relevance to adk-cc |
|---|---|---|
| Sep, 2.1.257–.266 | **Fable 5.1** default (1M ctx); `/diff` fullscreen panel; `/skill-doctor` (unused skills); `bashOutputMaxChars`/`taskOutputMaxChars` (≤128K); `PreModelSwitch`/`PostModelSwitch` hooks; `--permission-prompts none`; `managedMcpServers`; `--restricted` mode (no exec/WebFetch); per-agent `cacheTtl`; prompt-cache miss reasons in `/cost`; "Containment Escape" auto-mode rule; 1 GB cap on tool results on disk | `/skill-doctor` ≈ our skill_usage counters; output caps ≈ our microcompact; containment-escape rule maps onto our command classifier |
| Aug, W32–W34 | **Cross-session messaging** (`@session` mentions, sessions message each other); **`/design`** artboards (Claude Design in CLI/Desktop); **Concise output style**; **fork mode default** (subagent inherits full conversation); auto-continue after usage limit; GitLab MR support; `ANTHROPIC_DEFAULT_MODEL`; **auto mode default** on Pro/Max/Team (Aug 14); allow/deny rules "as sentences" | cross-session messaging vs our parked agent-control interface; concise style vs our Style section |
| Jul, W27–W30 | **Opus 5 / Sonnet 5** defaults; **in-app browser on Desktop**; **iOS Simulator pane**; `/fork` (copy conversation to background session); **screen reader mode**; `/doctor` full checkup; **Claude Security plugin** (multi-agent vuln scan → patches); `/code-review` as background subagent; artifacts call viewers' MCP connectors; subagents background by default | in-app browser/simulator ≈ our #132 screenshot affordance; security plugin ≈ our command-safety review |
| Jun, W23–W26 | **`/cd`** (move working dir mid-session, cache kept); **sub-agents spawn sub-agents** (5 deep); `--safe-mode`; `fallbackModel` (up to 3); **Artifacts** (live shareable page); **param-matched rules** `Tool(param:value)`; `/config key=value`; auto mode blocks destructive git; `claude mcp login/logout`; shell mode reacts to `! cmd` output; `/rewind` past `/clear`; background subagents surface permission prompts in main session | param-matched rules ≈ our allow-rule broadening (#79); fallbackModel ≈ our SelectableLlm registry; background-subagent prompts ≈ our confirmation waves (#114) |
| May, W19–W22 | **Opus 4.8**, `/effort xhigh`; **dynamic workflows** (script orchestrates 10s–100s of subagents); security-guidance plugin; **auto mode on Pro**; `/usage` breakdown by skill/subagent/plugin/MCP; **`/code-review`**; background sessions in `/resume`; **`claude agents` view**; **`/goal`** (persist until condition holds); Rewind "Summarize up to here"; plugins from `.zip`/URL; `worktree.baseRef`; auto-mode hard deny rules; hooks see effort level | `/usage` breakdown ≈ our quotas plugin; `/goal` ≈ our verify-gate loop; "Summarize up to here" ≈ our guided /compact (#128) |
| Apr, W15–W18 | **Ultraplan** (cloud plan, web review); **Monitor tool** (stream background events into conversation); `/loop` self-pacing; `/team-onboarding`; `/autofix-pr`; **Opus 4.7**, `/effort` slider; **Routines** (scheduled cloud agents); mobile push; **native binaries**; `/ultrareview` public preview; session recap; custom themes; Windows without Git Bash (PowerShell tool); `claude ultrareview` for CI; `claude project purge` | Monitor tool ≈ our process registry/log tail (#108/#131) — the closest upstream analogue to what we built |
| Mar 23–Apr 3, W13–W14 (partly in snapshot) | **Auto mode** (classifier handles permission prompts); computer use in CLI; `/powerup`; PowerShell tool; conditional `if` hooks; transcript search | auto mode ≈ our command classifier + #122 sandbox relaxation |

## Threads worth watching for adk-cc
- **Auto mode as the default permission posture** (classifier + hard deny + containment-escape + destructive-git rules). Our danger classifier covers the deny side; upstream's "safe actions run without interruption" is the allow side we approximate with read-only auto-allow.
- **Monitor tool + background subagents by default + fork mode.** Our #108/#131 process registry and #130 librarian are the equivalents; the "stream events into the conversation" shape (Monitor) is the piece we don't have.
- **Cross-session messaging / `claude agents` view** — overlaps the parked agent-control interface; upstream chose sessions-as-peers.
- **Artifacts** (live shareable pages + MCP connectors) — no analogue; our artifact panel is per-session.
- **`/skill-doctor`, `/usage` breakdown, prompt-cache diagnostics** — cheap observability wins that map onto data we already record (skill_usage, quotas, cost tracker).

Sources: changelog (github.com/anthropics/claude-code CHANGELOG.md), docs What's-new
(code.claude.com/docs/en/whats-new), npm registry (`@anthropic-ai/claude-code` time/pack).
