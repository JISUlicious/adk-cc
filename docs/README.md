# adk-cc documentation

Scope: this directory documents the **adk-cc implementation** only. It does not describe upstream Claude Code's architecture except where directly relevant (e.g. citing a prompt's lineage).

- [`01-specification.md`](./01-specification.md) — what adk-cc is, what it does, what's in scope.
- [`02-architecture.md`](./02-architecture.md) — how it's built: agent topology, the ADK mechanisms used to enforce coordinator-owns-I/O, file layout.
- [`03-prompts.md`](./03-prompts.md) — per-agent prompt structure and the upstream sources each one is ported from.
- [`04-deployment-sandbox.md`](./04-deployment-sandbox.md) — operator runbook: provisioning the sandbox VM, configuring Docker (plain TCP or mTLS), wiring the agent.
