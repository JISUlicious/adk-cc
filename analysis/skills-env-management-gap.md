# Env & Secret Management for Skills/MCP — Research + Gap Analysis

Status: research complete · Date: 2026-06-26
Companion: [skills-env-management-plan.md](./skills-env-management-plan.md)

## Question

We want **per-user** env/secret management: a user sets the credentials a
skill or MCP server needs (from a UI), and those values reach the agent's
in-sandbox commands. Before building, two things had to be answered:

1. Is there a **standardized** method for declaring/managing env in skills we
   should adopt rather than invent?
2. What does adk-cc already have, and what's the precise gap?

## TL;DR

- **There is no standard for declaring env/secrets in skills.** The open
  Agent Skills spec (agentskills.io), Anthropic's executable-skills proposal,
  and Claude Code all lack a declarative env/secret field. This is a genuine
  ecosystem gap, confirmed at the source.
- The spec *does* sanction a place for client-specific declarations:
  **`metadata`** (arbitrary namespaced key→value). That's where ours goes —
  staying 100% spec-compliant and portable.
- The only mature, shipped **declare → prompt → store-securely → inject**
  pattern is **VS Code MCP `inputs`** (`${input:id}`, `password: true`, OS
  keychain). Adopt its *shape* as the value of our `metadata` key.
- adk-cc already has ~80% of the machinery (`CredentialProvider` +
  encrypted-file store + admin UI + MCP `credential_key` + `sandbox_env`).
  The real gaps: **(a) the store is per-tenant, not per-user**, **(b) no skill
  declaration**, **(c) no user-facing settings flow**, **(d) sandbox env
  injection is wired only to Daytona and only at create-time** (no on-demand).

## What the standard actually says

### Agent Skills open spec (agentskills.io/specification)

THE open format (Anthropic-originated, ~40 adopters: Cursor, VS Code, Gemini
CLI, Codex, Goose, OpenCode, …). Complete frontmatter — six fields:

| Field | Req | Purpose |
|---|---|---|
| `name` | ✅ | identifier (dir-matched, kebab) |
| `description` | ✅ | what / when |
| `license` | — | license |
| `compatibility` | — | **prose** env hint ≤500 chars ("Requires git, docker, and internet") |
| `metadata` | — | **arbitrary string map**; *"Clients can use this to store additional properties not defined by the spec"* |
| `allowed-tools` | — | space-separated pre-approved tools (experimental) |

Verified that ADK's own `google.adk.skills.Frontmatter` exposes exactly these
(`name, description, license, compatibility, allowed_tools, metadata`) — so we
can read `metadata` today with no ADK change.

**Finding:** no env/secret/config field. `compatibility` is documentation
prose, not machine-readable. `metadata` is the sanctioned extension point, and
the spec explicitly recommends namespacing keys to avoid collisions.

### Executable Agent Skills proposal (anthropics/skills#157)

Adds `from`/`build`/`command`/`inputSchema`/`outputSchema`. Secrets get one
line — *"Injected at runtime via environment variables; never written to disk
or logs"* — but **no declarative schema**; the author flags it as a gap.

### MCP config (standard + Claude Code)

- Standard `.mcp.json`: per-server `env` block, literal `KEY=VALUE`,
  per-process scoping, **no substitution**.
- Claude Code extends with `$VAR`/`${VAR}` expansion from the process env.
  Known footguns: `claude mcp add` resolves placeholders and **writes secrets
  back to disk** (anthropics/claude-code#18692); no secure store; no prompt
  (open: #2065 secure env, #28942 `envFile`).

### VS Code MCP `inputs` — the pattern to mirror

```jsonc
"inputs": [
  { "type": "promptString", "id": "perplexity-key",
    "description": "Perplexity API Key", "password": true }
],
"servers": { "perplexity": { "command": "npx", "args": ["..."],
    "env": { "PERPLEXITY_API_KEY": "${input:perplexity-key}" } } }
```

Declare (`id`/`description`/`password`/`type`) → bind via `${input:id}` →
**prompt once when first needed, store in OS secure storage, remember** →
inject into the server `env`. Config stays VCS-safe. This is exactly the UX we
want; we reuse its *shape* (`id`/`description`/`secret`), not its storage.

## What adk-cc has today

| Piece | Location | Notes |
|---|---|---|
| Secret store ABC | `credentials/provider.py` | `get/put/delete/list_keys(*, tenant_id, key)` |
| Encrypted-file impl | `credentials/impls.py` | `<root>/<tenant_id>/<key>.enc`, Fernet; in-memory variant too |
| MCP secret ref | `tools/mcp.py` (`credential_key`), `tools/mcp_tenant.py` | static path = env-var name; tenant path = provider key → bearer header |
| Sandbox env injection | `sandbox/sandbox_env.py` | `SandboxEnvSpec` (passthrough/static/credentials) |
| Admin credential UI | `service/admin_routes.py` | GET/PUT/DELETE `/tenants/{tid}/credentials` (names only, no values) |
| Self-service routes | `service/identity_routes.py` | `/auth/me`, `/auth/profile`, `/auth/api-keys` — the pattern to mirror |
| Identity / per-user auth | `service/identity_routes.py`, tenancy plugin | authenticated principal available per session |

## Gap analysis — adk-cc vs. the standard/best-practice

| Capability | Standard / best-of-breed | adk-cc today | Gap |
|---|---|---|---|
| Secret store | OS keychain / env | `CredentialProvider`, **encrypted, server-side** | ✅ have it — and better |
| **Scope** | per-user (VS Code) | **per-tenant only** | ❌ **no per-user** — primary need |
| Declaration (MCP) | `inputs[]` + `${input:id}` | `credential_key` (key name only) | ⚠️ half — no description/secret/prompt metadata |
| Declaration (skills) | *none official* | *none* | ❌ missing — define via `metadata.<ns>` (spec-compliant) |
| UI prompt + remember | prompt once, store | admin lists tenant keys; no user flow | ❌ missing user settings flow |
| Injection into sandbox | per-process `env` | `sandbox_env` | ⚠️ **Daytona-only, create-time-only** |
| **On-demand injection** | per-exec env (process spawn) | — (baked once at create) | ❌ **secrets set after create never reach a running sandbox** |
| Anti-leak hygiene | Claude Code *writes secrets to disk* | never logged, names-only, no write-back | ✅ ahead — preserve |
| **Value isolation** (not in model I/O, session DB, or UI) | per-process env keeps it out of model context | partial (names-only store/UI); **no redaction of tool output / model I/O / events DB yet** | ❌ add `SecretStr` + `SecretRedactionPlugin` |

## Decisions

- **Layering: user-over-tenant (LOCKED).** A user's personal secret wins; falls
  back to the tenant/org-shared secret; falls back to the MCP static-path env
  var. Covers "my personal key" *and* "the team's shared key" with one
  mechanism, near-zero extra cost vs. user-only.
- **Declaration home: `metadata["x-adk-cc/secrets"]`** in `SKILL.md` (namespaced,
  spec-compliant, ignored by other agents, passes `skills-ref validate`). MCP
  keeps `credential_key`, enriched with an optional description; both feed one
  "required inputs" registry.
- **Declaration shape: VS Code `inputs`** → `{ id, description, secret }`.
- **On-demand injection required.** Env resolution moves from create-time-only
  to **resolve-at-exec** (with a short TTL + an invalidation signal on secret
  change), so a secret a user provides mid-session reaches the next command
  without recreating the sandbox. Create-time injection stays as an
  optimization for backends that prefer it.
- **Secret hygiene is a hard invariant (LOCKED).** A resolved value lives in
  exactly one flow (`store → resolver → exec env / MCP header`) and must NEVER
  appear in: the model input (LlmRequest), model/tool output, the session
  store (state + events DB), or anything delivered to the user. Enforced by
  **isolation by construction** (the model sees only `id`/`description`; a
  `SecretStr` type hides the raw value) + **egress redaction** (a
  `SecretRedactionPlugin` scrubs tool results / responses before they reach the
  model, the DB, or the UI). Redaction stops *accidental* leakage, not a
  malicious skill deliberately exfiltrating — that's the skill-trust boundary.

## Why this is the right altitude

- We do **not** invent a non-standard top-level frontmatter field (that would
  fail spec validation and break portability). We use the spec's own escape
  hatch.
- We **extend** the existing `CredentialProvider` with one `user_id` dimension
  rather than building a parallel personal-secrets system.
- Env **resolution** centralizes (one pipeline for MCP + skills + sandbox,
  create-time *and* on-demand); env **application** stays a per-backend
  primitive (each create/exec API differs) — matching the sandbox ABC's
  existing policy/mechanism split.
- We keep adk-cc's security edge (encrypted at rest, names-only listing, no
  disk write-back) that Claude Code itself lacks.

## Sources

- Agent Skills — Specification — https://agentskills.io/specification
- Agent Skills — Overview / Client Showcase — https://agentskills.io/home
- Equipping agents for the real world with Agent Skills — Anthropic — https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Proposal: Executable Agent Skills · anthropics/skills#157 — https://github.com/anthropics/skills/issues/157
- Command/skill frontmatter reference — anthropics/claude-code — https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/command-development/references/frontmatter-reference.md
- Connect Claude Code to tools via MCP — https://code.claude.com/docs/en/mcp
- `claude mcp add` writes resolved secrets to disk · anthropics/claude-code#18692 — https://github.com/anthropics/claude-code/issues/18692
- Securely provide env vars to MCP servers · anthropics/claude-code#2065 — https://github.com/anthropics/claude-code/issues/2065
- MCP configuration reference (`inputs`/`${input:id}`) — VS Code — https://code.visualstudio.com/docs/agents/reference/mcp-configuration
- MCP JSON Configuration — FastMCP — https://gofastmcp.com/integrations/mcp-json-configuration
