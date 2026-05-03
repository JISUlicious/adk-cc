# adk-cc documentation

Scope: this directory documents the **adk-cc implementation** only. It does not describe upstream Claude Code's architecture except where directly relevant (e.g. citing a prompt's lineage).

- [`01-specification.md`](./01-specification.md) — what adk-cc is, what it does, what's in scope. Roles, behavior contract, constraints, deferred items.
- [`02-architecture.md`](./02-architecture.md) — how it's built: file layout, agent topology, the dual ADK mechanism that enforces "coordinator owns user I/O", plan-mode-as-posture, sandbox layer, task tracking, the runtime tool-call validator.
- [`03-prompts.md`](./03-prompts.md) — per-agent prompt structure and the upstream sources each one is ported from. Includes the dynamically-injected `PLAN_MODE_REMINDER`.
- [`04-deployment-sandbox.md`](./04-deployment-sandbox.md) — sandbox VM operator runbook: provisioning the host, configuring Docker (plain TCP or mTLS), wiring the agent.
- [`05-production-deployment.md`](./05-production-deployment.md) — end-to-end production runbook + readiness checklist. Topology, deployment steps, custom auth, day-2 ops, and the alpha-status gap list (security / reliability / observability / ops / multi-tenancy / config / tests).
