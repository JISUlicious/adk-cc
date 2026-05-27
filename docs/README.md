# adk-cc documentation

Read in: **English** · [한국어](./README.ko.md)

Scope: this directory documents the **adk-cc implementation** only. It does not describe upstream Claude Code's architecture except where directly relevant (e.g. citing a prompt's lineage). Each file below is also available in Korean under the same name with the `.ko.md` suffix.

- [`01-specification.md`](./01-specification.md) — what adk-cc is, what it does, what's in scope. Roles, behavior contract, constraints, deferred items.
- [`02-architecture.md`](./02-architecture.md) — how it's built: file layout, agent topology, the dual ADK mechanism that enforces "coordinator owns user I/O", plan-mode-as-posture, sandbox layer, task tracking, the runtime tool-call validator.
- [`03-prompts.md`](./03-prompts.md) — per-agent prompt structure and the upstream sources each one is ported from. Includes the dynamically-injected `PLAN_MODE_REMINDER`.
- [`04-deployment-sandbox.md`](./04-deployment-sandbox.md) — sandbox operator runbook: provisioning a Docker-based sandbox host (plain TCP or mTLS) **or** standing up the `sandbox_service` backend against an external REST sandbox (gVisor-isolated, e.g. [JISUlicious/sandboxing](https://github.com/JISUlicious/sandboxing)).
- [`05-production-deployment.md`](./05-production-deployment.md) — end-to-end deployment runbook + readiness checklist. Topology, deployment steps, custom auth, workspace storage tiers (Tier 1 single-host through Tier 4 service-mediated), day-2 ops, and the alpha-status gap list (security / reliability / observability / ops / multi-tenancy / config / tests).
- [`06-confirmation-protocol.md`](./06-confirmation-protocol.md) — wire protocol for the tool-confirmation HITL prompt: outbound `ConfirmPrompt` payload shape, inbound `chose_id` values, "Allow always" session-rule scoping, and the legacy `confirmed: bool` fallback for frontends that don't speak the payload protocol.
- [`07-web-ui.md`](./07-web-ui.md) — React chat UI runbook: stack, source layout, event flow, long-running tool resume protocol, wire format quirks, slash commands, theme, dev + prod run modes, env knobs.
