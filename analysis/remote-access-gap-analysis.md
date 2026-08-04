# Remote access to running agent sessions — market benchmark & adk-cc gap analysis

**Date:** 2026-07-19 · **Method:** deep-research workflow (5 search angles → 23 sources
fetched → 115 claims extracted → 25 top claims adversarially verified, 23 confirmed /
2 refuted) + direct journal extraction for the OSS products whose claims fell outside
the verification budget.

**Confidence markers used below:**
- **[V]** — survived 3-vote adversarial verification (3-0 or 2-1) against primary docs
- **[D]** — extracted verbatim from primary docs/repos (fetched 2026-07-19), not
  adversarially verified
- **[S]** — secondary/press/practitioner source

Everything here is Feb–Jul 2026 vintage; this space moved monthly through 2025-26 —
re-check before relying on any claim past ~Q3 2026.

**Corrections the research surfaced (vs the brief):**
- "pi-mono" is stale: the repo moved to **earendil-works/pi** (72.3k stars, v0.80.10
  2026-07-16); the former web-ui package is gone from the monorepo and the `mom` Slack
  bot was deleted 2026-04-30; chat surfaces live in a separate `earendil-works/pi-chat`. [D]
- Nothing found contradicted the stated adk-cc current state.
- Refuted claims (0-3): Codex auth is NOT just the ChatGPT account (per-device QR
  pairing is required); "delegate Codex cloud tasks *without leaving* GitHub/Linear/
  Slack" is overstated (tasks can be *initiated from* those tools).

---

## 1. Benchmark matrix

| Dimension | Claude Code | OpenAI Codex | Hermes agent | pi (earendil-works) | OpenClaw |
|---|---|---|---|---|---|
| **Surfaces** | Terminal, desktop app, claude.ai/code web, Claude iOS/Android, Slack (Claude Tag), Channels plugins (Telegram/Discord/iMessage) [V] | CLI, IDE, ChatGPT web, ChatGPT iOS/Android (Codex Remote + cloud tasks), GitHub/Linear/Slack initiation [V] | 20+ chat platforms via gateway (Telegram, Discord, Slack, WhatsApp, Signal, SMS, Email, Matrix, Teams, LINE, iMessage/BlueBubbles, ntfy) + third-party hermes-webui browser PWA [D] | Terminal only (TUI/CLI); RPC = stdin/stdout JSONL for local embedding; chat split to pi-chat repo [D] | WhatsApp, Telegram, Slack, Discord, Signal, iMessage, WebChat + macOS/iOS/Android "node" clients [D] |
| **Where the session lives** | Local machine (Remote Control) OR Anthropic cloud VM (web / Claude Tag) [V] | Local/SSH host (Codex Remote) OR OpenAI cloud container (Codex cloud) [V] | Self-hosted host; exec can be remote (local/Docker/SSH/Singularity/Modal/Daytona backends) [D] | Local machine only [D] | Self-hosted host (single Gateway daemon) [D] |
| **Transport / NAT story** | Vendor relay: local session outbound-HTTPS only, registers + polls, server routes streaming messages; transcript stored server-side; ~10-min outage tolerance [V] | Vendor "secure relay"; host never exposed; live state loads onto phone [V] | Gateway dials out to messaging platforms; no inbound ports, no relay needed [D] | None (stdin/stdout) [D] | Loopback WS+HTTP on :18789; NO built-in relay — Tailscale/SSH tunnel recommended [D] |
| **Same-session multi-device attach** | YES — terminal + browser + phone steer ONE live session; subagent progress synced [V] | YES — phone attaches to live host state incl. threads/approvals/plugins [V] | Per-chat sessions persist on host; cross-platform continuity of the SAME chat session; webui imports CLI sessions (handoff) [D] | NO — export/import (HTML/JSONL, gist) only [D] | Per-conversation sessions on host; any paired control-plane client attaches [D] |
| **Handoff between surfaces** | Asymmetric: `--teleport` pulls cloud→terminal; CLI can't push local→web (`--cloud` = new session); Desktop "Continue in" pushes to web [V] | Cloud tasks forkable from phone; local↔cloud not unified (two documented paths) [V] | CLI→web import (hermes-webui) [D] | Static export only [D] | n/a (sessions are chat-scoped) |
| **Remote steering** | Send/interrupt from any attached device [V] | Steer mid-run, approve, change models from phone [V] | interrupt / queue / steer modes from chat; /background spawns async sessions [D] | RPC has steer/abort/follow_up primitives — but only for a LOCAL embedding client [D] | Message-driven per chat; nodes declare capabilities [D] |
| **Remote approvals (HITL)** | First-class: push "when actions required", approve from phone; CLI nudges "Approve tool calls from your phone" (v2.1.208+) [V] | Approve/deny commands from phone (GA 2026-06-25) [V] | /approve + /deny in the chat surface for dangerous commands [D] | NONE — pi has no permission system at all (structurally empty dimension) [D] | Exec approvals via tool policy; approvals in chat/control UI [D] |
| **Notifications** | Two push toggles: "when Claude decides" / "when actions required"; skipped while user types in terminal [V] | Real-time streaming to phone (output, diffs, approval prompts) [V] | Operator notifications to each platform's home channel (restarts, circuit-breaker, tool progress) [D] | None [D] | Messages arrive in the chat apps themselves [D] |
| **Auth & pairing** | claude.ai OAuth + session URL/QR; short-lived single-purpose scoped credentials; optional Trusted Devices (per-device enrollment + biometric step-up) [V] | Per-device QR pairing through the relay (account alone NOT sufficient — refuted 0-3) [V] | Deny-by-default: per-platform allowlists OR DM pairing (one-time code, 1h expiry, rate-limited, host-CLI approval); admin/user tiers [D] | n/a | Device-identity pairing: new device IDs need explicit approval → device token + challenge-nonce signing; DM pairing (1h codes, 3/channel cap); gateway token by default; `auth.mode:none` exists with loud warnings [D] |
| **Self-host vs vendor cloud** | Vendor relay mandatory (no API-key/Bedrock/Vertex support; no self-hosted relay — issue #25746 closed not-planned) [V][S] | Vendor relay mandatory [V] | Fully self-hosted [D] | Fully local [D] | Fully self-hosted [D] |
| **Gating / maturity** | Remote Control, web, Channels = research previews; Pro/Max/Team/Enterprise; off-by-default for Team/Enterprise. Claude Tag: beta, Enterprise/Team ONLY [V] | Codex Remote GA 2026-06-25 (open Windows/SSH/mobile-sync bugs); cloud plan-gated, ~5× credit cost [V] | OSS | OSS | OSS; 180k+ stars by Jan 2026 [S] |

---

## 2. The four recurring architectures

**A. Vendor-cloud session host** (Claude Code web, Codex cloud, Claude Tag).
Sessions run on vendor VMs; every device is a view; persistence independent of any
device. Trade: zero user infra, instant mobile; but vendor lock, plan gating, no
local filesystem, ~5× cost (Codex), and for Claude Tag no per-run HITL yet.

**B. Local host + outbound relay** (Claude Remote Control, Codex Remote; OSS: amux
tunnel). *The standout pattern and the industry's answer to "reach a loopback-only
agent".* The session host makes **outbound-only** HTTPS/WS to a relay, registers,
polls; remote clients route through the relay to the SAME live session. NAT solved
without inbound ports. Costs: vendor dependency + transcripts transiting/stored on
vendor servers (Anthropic documents this; OpenAI's retention undocumented), pairing
discipline required (QR / short-lived scoped credentials).

**C. Messaging gateway on the session host** (OpenClaw, Hermes gateway, Claude
Channels, pi's deleted `mom` bot). One daemon owns platform connections — all
*outbound* (Slack Socket Mode WebSocket, Telegram long-poll, Baileys, grammY) — so
NAT is solved by the platforms themselves; per-chat sessions; pairing via one-time
codes; approvals as chat commands (/approve, /deny). Trade: the messaging platform
becomes your transport dependency + delegated-authority risk (anyone who can message
the bot can induce tool calls — prompt injection mitigated by tool policy/approvals/
allowlists, not prompts [D]).

**D. Self-hosted web on LAN/VPN** (hermes-webui, OpenCode-behind-Tailscale, amux
dashboard). Responsive/PWA web UI on the session host; reachability via Tailscale
Serve / SSH tunnel; auth optional (and therein the danger). Trade: no vendor, full
privacy; NAT/auth left to the user; no push unless added (ntfy et al.).

**Security lessons (mostly from OpenClaw, the cautionary reference):**
- **1,800+ exposed instances** leaking API keys/chat histories (Jan 2026 scans);
  Shodan-fingerprintable control UI; 8 manually-examined instances gave full
  unauthenticated command execution [S].
- Root cause: **trust-localhost architecture** — reverse proxies (nginx/Caddy) make
  external requests appear to come from 127.0.0.1 → trusted. Patched vector, unchanged
  architecture [S]. **This is exactly adk-cc desktop's model today** (loopback sidecar,
  no auth): safe only until someone proxies it.
- Supply chain: ClawHavoc (hundreds of malicious ClawHub skills, Atomic Stealer,
  persistent MEMORY.md poisoning) [S] — relevant to adk-cc's skills registry ambitions.
- amux ships **no auth** and defaults to **auto-approve** ("YOLO mode") — it sidesteps
  remote HITL rather than solving it [D]. Anti-pattern to avoid.
- The good examples: Hermes' deny-by-default pairing (one-time codes, expiry,
  rate-limits, host-side approval) and OpenClaw's device-token + challenge-nonce
  handshake with explicit non-goals (single-principal; "not a security boundary for
  adversarial users sharing one agent" [D]).

---

## 3. Gap analysis: adk-cc vs the market

adk-cc ground truth (per repo, not re-verified by the research): multi-tenant web
service (FastAPI, Bearer auth, server-side ADK sessions, SSE, in-stream tool
confirmations) + single-user desktop (Tauri, loopback sidecar, NO auth) + just-shipped
SSH remote *workspaces* (inverse direction: remote exec, local UI).

| Capability | Market table stakes (2026) | adk-cc today | Gap size |
|---|---|---|---|
| Remote reachability of sessions | Relay or cloud host | **Web mode: already reachable** (server-side sessions); desktop: loopback-only | Web: none · Desktop: large |
| Same-session multi-device attach | CC/Codex both do live attach | **Web mode: implicitly yes** (any logged-in browser sees the same server-side session; SSE streams to all) — un-marketed but architecturally present | Small (UX polish) |
| Mobile-usable UI | Native apps (vendors) or responsive PWA (OSS) | React UI is desktop-oriented; no PWA manifest | Medium |
| Push notifications | Both vendors: push on "action required"/completion | None (SSE only — requires open tab) | Medium |
| Remote approvals | Approve-from-phone is table stakes (CC toggle, Codex GA, Hermes /approve) | Confirmation cards render in web UI only; no away-device delivery | Medium (delivery), small (UI exists) |
| Desktop remote access | Vendors: outbound relay + pairing; OSS: tunnel/VPN | None; **and no auth to gate any future exposure** | Large — auth is the prerequisite |
| Messaging surfaces | CC Channels/Tag, Hermes 20+, OpenClaw 7 | None | Large (but plugin seams fit) |
| Session handoff local↔server | CC teleport (asymmetric), hermes-webui import | None (desktop and web sessions are separate worlds) | Medium, deferrable |
| Device pairing / scoped tokens | QR + short-lived scoped creds (vendors), one-time codes (OSS) | Web: long-lived Bearer in localStorage; desktop: nothing | Medium |

**Framing insight:** the market's hard problem — "my agent runs on a machine behind
NAT, how does my phone reach it" — is a problem adk-cc's **web mode does not have**
(sessions already live on a reachable server; the browser is already a thin client).
adk-cc's web mode is architecturally *ahead* of pattern-B vendors for that half of the
product: what's missing is entirely presentation-layer (mobile UI, push, approval
delivery). The desktop app has the full-size version of the problem, and it is exactly
the configuration both vendors refused to expose directly and OpenClaw got burned by.

---

## 4. Phased recommendations

**Phase 0 — desktop auth/pairing (prerequisite, do before ANY exposure).**
Add a sidecar token: generated at first run, stored in the desktop data dir, required
on every non-loopback request (and ideally loopback too — the OpenClaw
reverse-proxy-launders-as-localhost lesson). Device pairing for future remote clients:
one-time code shown in the desktop app, exchanged for a per-device token (Hermes/
OpenClaw pattern; QR later). Small, self-contained PR; unblocks everything else.

**Phase 1 — mobile-usable web mode (highest value/cost).**
Responsive pass on the React UI (ChatPage, composer, confirmation cards, rail) + PWA
manifest/installability. hermes-webui's bar (≤640px layout, 44px targets, PWA) is the
reference. adk-cc web then already delivers pattern-D + same-session attach with zero
new backend.

**Phase 2 — notifications + approve-from-phone (web mode).**
Web Push (VAPID) wired to exactly two events, mirroring Claude Code's two toggles:
*action required* (ADK tool-confirmation emitted) and *turn finished/attention*. The
approval UI already exists in the SSE stream; push is just the away-device doorbell.
Optional ntfy webhook as the self-hosted escape hatch (practitioner standard [S]).
Respect the model-endpoint rate-limit rule: notification fan-out must not trigger
model calls.

**Phase 3 — desktop reachability via the web service as a self-hosted relay.**
Recommended architecture: reuse adk-cc's OWN multi-tenant web service as the relay —
the desktop app dials OUT (registers + long-polls/WS, mirroring Anthropic's design
[V]), the web service routes messages between remote browsers/phones and the desktop
session; per-device pairing from Phase 0; short-lived scoped credentials. This is
pattern B without the vendor: transcripts stay on the user's own server. Interim v1
(cheap, ship first): document the Tailscale-Serve/SSH-tunnel path over the
Phase-0-authenticated sidecar — identical to hermes-webui's supported path [D], zero
new code beyond auth.

**Phase 4 — messaging gateway plugin (Telegram first).**
An OpenClaw/Hermes-style gateway as an adk-cc service + BasePlugin: outbound
long-poll (no inbound ports), chat↔session mapping (per-chat session keyed like
Hermes/OpenClaw [D]), deny-by-default pairing with one-time codes, and /approve //deny
mapped onto the EXISTING ADK tool-confirmation flow (Hermes proves this mapping [D]).
Telegram first (simplest outbound API), Slack Socket Mode second (the pi `mom`
write-up documents the exact token model + its 10-connection cap [D]). Guardrails from
day one: per-sender session isolation, requireMention in groups, tool-policy floor
unchanged — the permission engine already provides the deny/ask floors.

**Phase 5 (deferred) — session handoff desktop↔web.** Only if demand appears;
CC's asymmetric teleport shows even vendors haven't fully solved it, and Phases 1-3
make it mostly unnecessary (attach beats handoff).

---

## 5. Sources

Primary (fetched live 2026-07-19): code.claude.com/docs (remote-control,
claude-code-on-the-web, desktop, channels), anthropic.com/news/introducing-claude-tag,
openai.com/index/work-with-codex-from-anywhere, developers.openai.com/codex
(cloud/changelog/ide), learn.chatgpt.com/docs (remote-connections, cloud),
help.openai.com, docs.openclaw.ai (concepts/architecture, gateway/security),
hermes-agent.nousresearch.com/docs (messaging), github.com/NousResearch/hermes-agent,
github.com/nesquena/hermes-webui, github.com/earendil-works/pi (ex badlogic/pi-mono),
github.com/mixpeek/amux, github.com/yuuichieguchi/claude-remote-approver, Boris
Cherny (Threads, 2026-03-29).

Secondary/press: Help Net Security 2026-02-25 (Remote Control), Security Boulevard
07/2026 (Claude Tag access model), TechCrunch/Fortune/VentureBeat 2026-06-23 (Tag
launch), MacRumors 2026-05-15 (Codex mobile), Nebius blog + VentureBeat (OpenClaw
security incidents), dev.to practitioner stack write-ups.

Verification stats: 25 claims voted (3 skeptic votes each) → 23 confirmed
(19× 3-0, 2× 2-1), 2 refuted; OSS-product claims are primary-doc extractions that
fell outside the 25-claim verification budget (marked [D] throughout).
