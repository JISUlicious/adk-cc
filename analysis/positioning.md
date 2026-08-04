# adk-cc positioning — honest competitive read (2026-07-19)

Grounded in a survey of the OSS coding-agent field (OpenCode, pi, OpenHands, Aider,
Cline, Roo, Goose, Continue, Plandex, Kilo, SWE-agent, Hermes, Tabby, bolt.diy, +
Refact/gptme/Cody). Companion: [`remote-access-gap-analysis.md`],
[`agent-control-interface.md`].

## Verdict: packaging, not a moat

adk-cc is **not differentiated on features** — it is differentiated (weakly-to-
moderately, defensible only by execution quality) on **packaging + completeness**: it is
the one OSS codebase that is simultaneously a real single-user desktop app AND a real
multi-tenant, code-executing, per-tenant-isolated web service. Do not oversell this as a
technological moat — the hard half is reproducible (Plandex proves it) and the desktop
half is commodity.

## What is table stakes (NOT a selling point vs OSS)

Provider-agnostic / BYO-model; MCP + skills; SSE streaming; CLI + loopback single-user;
git-native **shadow-git checkpoint/undo (Cline and Roo ship the identical mechanism)**;
Docker/container sandbox exec; client/server split; even cloud sandboxes
(Daytona/e2b/Modal). adk-cc's ADK core, Tauri shell, in-place host workspace all fall
here. None is a differentiator.

## The two genuinely uncommon axes (both integration, not features)

1. **Dual-mode from one codebase — single-user desktop GUI app + true multi-tenant web
   service.** Effectively unique in OSS. Goose has a desktop app but is single-tenant
   (shared key); OpenHands has multi-tenant Cloud but no desktop and the MT layer is
   proprietary; Plandex forks single↔multi-user from one codebase (closest precedent)
   but is CLI-only.
2. **An OSS *code-executing* agent with real per-user isolation of secrets + MCP + skills
   + workspaces.** The field splits: the well-isolated multi-tenant OSS tool (Tabby)
   does NOT execute code; the OSS multi-user executor (Plandex) has thin per-user
   isolation (client-side edits, no per-user MCP/skills/secrets). adk-cc's per-user
   isolation inside an executing multi-tenant OSS service occupies an otherwise-empty
   niche.

## What is parity / behind

- **Execution-location spectrum:** adk-cc's six backends (noop/local-container/
  remote-docker/daytona/e2b/ssh) MATCH the broadest (Hermes' six) — not exceed. Not a
  moat.
- **Multi-tenant OSS is rare but NOT unfilled:** Plandex (MIT, executes, orgs+auth) and
  Tabby (Apache, SSO+ACL, no exec) already exist; everyone else keeps MT in a closed
  layer (OpenHands Cloud/Polyform, Continue Hub, Kilo/Roo Cloud).
- **Reach:** no mobile / push / messaging / remote session control yet (see
  remote-access-gap-analysis) — behind CC/Codex.

## Strategic implication

The moat is the NICHE, not the tech. Target buyer = a **team/org wanting a self-hosted,
multi-user, code-executing agent with real per-user isolation on its own models and
infra.** Against that buyer the OSS field thins to ~Plandex (thin isolation, no GUI) and
closed clouds. Do NOT fight OpenCode/Aider/pi for the solo-dev CLI user — you lose there
and the multi-tenancy is dead weight. Positioning line: **"a coding-agent service you
run, not rent — desktop for one, multi-tenant for a team, from one codebase."**

The parked **agent-control interface** (an open agent other agents drive through a shell)
is a genuine LEAD, not catch-up — no OSS CLI or commercial harness offers it.

Caveats: architectural facts are from primary repos (solid); 2026 corporate events
(Roo/Continue/Refact/Goose/OpenHands pivots) are lower-confidence in this future-dated
environment and were NOT used for the conclusions.
