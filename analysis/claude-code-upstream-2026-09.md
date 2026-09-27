# Claude Code upstream: what changed since our source snapshot (as of 2026-09-27)

**Our reference source** (`../src`, the March 31 2026 sourcemap leak) is
≈ v2.1.83–2.1.91: it references `ultrareview` and the `2.1.83` literal, and
predates the Monitor tool (v2.1.92). **Latest release: v2.1.283 (2026-09-25).**

**Newer source is NOT obtainable.** Since v2.1.105 (Week 16, April) the CLI
ships as a native binary; the npm tarball is still 7 files / 185 kB (re-checked 2026-09-27)
(`cli-wrapper.cjs`, `install.cjs`, `sdk-tools.d.ts`) — no JS bundle, no
sourcemap. Everything below comes from the public changelog and the weekly
"What's new" digests, not code.

## Update 2026-09-27 — v2.1.267 → v2.1.283 (17 releases in 17 days)

Note: 2.1.267–2.1.274 were published to npm but have **no CHANGELOG entries**
(their notes appear folded into 2.1.275). The docs' weekly digest stops at
Week 37 (Sep 7–11); Weeks 38–39 are not published yet.

| Version (date) | Feature / behavior change | Relevance to adk-cc |
|---|---|---|
| 2.1.280 (Sep 22) | **Claude Opus 5.5** (`claude-opus-5-5`, 1M ctx, $4/$20 per Mtok) — now the **default on Pro / Team Standard** (was Sonnet); effort defaults retuned for Opus 4.7/4.8/Fable 5 | new registry entry candidate; cheaper than Fable 5.1 ($10/$50) |
| 2.1.277 (Sep 18) | **AGENTS.md support** — read `AGENTS.md` when `CLAUDE.md` is absent | our repo already uses `CLAUDE.md → @AGENTS.md`; upstream now matches the convention natively |
| 2.1.277 / .278 / .282 / .283 | **Auto mode → server-side classifier by default** (API + Enterprise, then third-party providers start in auto; `CLAUDE_CODE_AUTO_MODE_SERVER=0` opts out; `/status` shows an "Auto mode server" row) | the allow-side counterpart of our command classifier is now a *server* call, not local rules |
| 2.1.277 | Subagent results reach the main agent **under a header marking them as subagent output**; deprecated **TaskOutput tool removed**; background Haiku auto-title dropped from `claude -p` | mirrors our specialist-report framing; our session-title plugin is the analogue of the dropped auto-title |
| 2.1.275 (Sep 17) | **Send-now key** (ctrl+enter) interrupts the current turn with a new message; **claude.ai account skills/plugins sync**; `/plugin install <p> --marketplace <src>`; hosted sessions keep an unanswered permission prompt up after restart; routine runs save to an artifact | "prompt kept after restart" = our parked-confirmation durability (#114/#119) |
| W37 (2.1.263–.269) | **`claude plugin eval`** (test cases + graders + no-plugin baseline; `eval init` drafts them); Desktop panes **pop out** into windows; **`maxEffortLevel`** caps effort on every provider; WebFetch 5-min hard timeout | plugin eval ≈ a measured A/B harness for skills — the thing we hand-roll per e2e |
| 2.1.283 (Sep 25) | **`/doctor prompt-audit`** (audits CLAUDE.md for outdated patterns); `availableModelsMatch` / `deniedModels` managed settings; MCP/WebFetch/WebSearch outputs in OTel spans; `mantle` Bedrock upstream; prompt suggestions back off after 20 unused | prompt-audit is a lint we could point at our own AGENTS.md |
| 2.1.282 (Sep 24) | **`maxProseWidth`**; `allowClaudeInChromeWithManagedMcp`; telemetry vars listed in `/status`; `sandbox.excludedCommands` ignores project/local entries; reserved `anthropic-skills:`/`claude-ai:` skill namespaces | project-level settings losing authority over sandbox exclusions = the same trust posture as our #116 |
| 2.1.281 (Sep 23) | `"attribution": false` in settings.json; MCP URL-mode elicitation (2026-07-28 protocol); `claude plugin validate` checks MCP servers; auto-mode recommendation in `/insights`; compaction spinner shows running token count; startup/first-request latency work | compaction token count ≈ our context gauge |
| 2.1.280 | `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`; hook output sizes in OTel; mouse support in fullscreen lists; `/cost` names thinking mode as a cache-miss cause | — |

### Threads to add to the watch-list
- **Opus 5.5 as the mass-market default** ($4/$20) — cheaper than Fable 5.1 at
  comparable context; worth a registry entry and a look at whether our
  `modelPicker`-equivalent should surface it.
- **Server-side auto-mode classifier** — Anthropic moved permission judgment
  into a service. Our local danger classifier is now the *conservative* design;
  the gap is the allow side (safe actions running without a prompt).
- **`claude plugin eval`** — first-party measurement harness for skills/plugins
  (cases, graders, baseline). Our e2e A/Bs do this by hand per feature.
- **`/doctor prompt-audit`** — run against AGENTS.md when convenient.

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
