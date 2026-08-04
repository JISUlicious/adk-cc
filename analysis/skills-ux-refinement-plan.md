# Skills: conformance to the standard, and what to refine

Revised 2026-07-31 (2nd pass) after reading the standard's own implementer
guide. The first pass treated authoring *recommendations* as if they were
limits — they are not, and the guide is explicit that a client should be
**lenient** even about the spec's hard constraints. Sources at the bottom.

## 1. Three different kinds of rule

The Agent Skills standard has two documents that matter to a runner, and they
say different things to different audiences.

**Tier A — hard constraints on the format** ([specification](https://agentskills.io/specification),
stated as *must*):

| Constraint | Value |
|---|---|
| `name` | required; 1–64 chars; lowercase `a-z0-9` and `-`; no leading/trailing/consecutive hyphens; **must match the parent directory** |
| `description` | required; 1–1024 chars; non-empty |
| `compatibility` | if present, 1–500 chars |
| `SKILL.md` | must exist; YAML frontmatter then Markdown body |

**Tier B — recommendations to skill *authors*** (stated as *should* /
*recommended* / *keep*). These are **not** limits a runner enforces:

| Guidance | Value |
|---|---|
| Instructions body | **< 5,000 tokens (recommended)** |
| `SKILL.md` length | **under 500 lines** |
| Catalogue metadata | ~50–100 tokens per skill |
| File references | one level deep; avoid nested reference chains |
| Scripts | self-contained, or clearly document their dependencies |
| Description content | say what it does *and* when to use it; include trigger keywords |

Anthropic's platform documentation states the same numbers independently (name
≤64 lowercase/digits/hyphens; description non-empty ≤1024; Level 1 ~100
tokens/Skill; Level 2 "under 5k tokens"), which is the cross-check for Tier A
and Tier B. It adds two rules that are **specific to uploading a Skill through
Anthropic's Skills API** and must NOT be imported into adk-cc's validator:
`name` may not contain XML tags or the reserved words "anthropic"/"claude", and
`description` may not contain XML tags. The reserved-word rule would reject
Anthropic's own published `claude-api` skill, which loads fine from the
filesystem — a clean illustration of why surface-specific validation does not
belong in a general runner.

**Tier C — what a *client* should do**, from
[adding skills support](https://agentskills.io/client-implementation/adding-skills-support).
This is the part the first pass got backwards. The guide prescribes **lenient
validation**:

| Situation | Prescribed client behaviour |
|---|---|
| `name` ≠ parent directory | **warn, load anyway** |
| `name` > 64 chars | **warn, load anyway** |
| description missing/empty | skip the skill, log the error |
| YAML unparseable | skip the skill, log the error |
| everything else | "record diagnostics … don't block skill loading on cosmetic issues" |

It also asks clients to: recover from the common malformed-YAML case (an
unquoted value containing a colon) by re-parsing with the value quoted; hide
filtered/disabled skills from the catalogue entirely; **exempt skill content
from context compaction**; consider **deduplicating repeat activations**; and
consider **gating project-level skills on a trust check**, because a cloned
repository can otherwise inject instructions into the agent's context.

## 2. Where adk-cc stands

Frontmatter fields (ADK parses all six faithfully; the question is use):

| Field | adk-cc |
|---|---|
| `name`, `description` | validated; over-long description truncated + warned rather than costing the skill |
| `metadata` | used (`x-adk-cc/secrets`) |
| `license` | parsed, never shown — 15 of 17 published skills set it |
| `compatibility` | parsed, **unused** |
| `allowed-tools` | parsed, **ignored, and nothing says so** |

Client behaviours from Tier C:

| Guide item | adk-cc |
|---|---|
| lenient on name-vs-directory | **No — the skill is rejected.** Reported in the panel since this week, but the guide says load it |
| lenient on over-long name | **No — rejected** |
| skip on missing description / bad YAML | Yes |
| malformed-YAML retry | **No** |
| hide disabled skills from the catalogue | Yes |
| exempt skill content from compaction | **Yes, by construction** — microcompact clears an *allowlist* (`run_bash`, `read_file`, …); `load_skill` is not in it |
| dedupe repeat activations | No |
| trust-gate project skills | **No** — `<project>/.adk-cc/skills` loads automatically |
| `.agents/skills/` interop path | **Not scanned** (we scan `.adk-cc/skills` + globals) |

Measured with a conformance linter over both corpora:

* adk-cc's 24 built-ins: **0 Tier-A violations, 0 Tier-B overruns.** None
  declare `license`, `compatibility` or `allowed-tools`.
* Published `example-skills` (17): **1 Tier-A violation** — `claude-api`,
  description 1,068 > 1,024. **2 Tier-B overruns** — `claude-api` (571 lines,
  ~18k tokens) and `skill-creator` (~8k tokens). Both are *published, working*
  skills, which is the point: Tier B is advice, and real skills exceed it.
* **Zero of 41 skills declare `compatibility` or `allowed-tools`.**

Our instruction cap is 60,000 chars ≈ 15,000 tokens. The first pass called that
"3× the spec ceiling" and proposed lowering it — **wrong**: 5,000 tokens is
advice to authors, and trimming there would break `claude-api`, which ships at
~18k. The cap is a runtime protection against a runaway file and should stay
generous; what matters is that it *reports* when it trims (it does).

## 3. Refinements

### R1. Adopt the guide's lenient loading, and report diagnostics *(detection, as asked)*
adk-cc is currently **stricter than the standard tells clients to be**. A skill
whose folder was renamed on install — the single most common real-world
malformation — is rejected outright, when the guide says warn and load.

* Load on name-vs-directory mismatch and over-64-char names; keep skipping only
  for a missing description or unparseable YAML, exactly as prescribed.
* Add the malformed-YAML retry (quote a value containing an unquoted colon and
  re-parse). This is the guide's own example and costs a few lines.
* Keep every one of these as a visible **warning** on the panel row, alongside
  the existing "not loaded" reason. Same surface, three severities: *failed to
  load* / *loaded with a fix applied* / *advisory*.
* Advisories come from Tier B and are addressed to the author, never enforced:
  body over 500 lines or ~5k tokens, description without a "use when" clause,
  reference chains more than one level deep.

The existing description repair already follows this philosophy — this
generalises it instead of leaving it a special case.

### R2. Use `compatibility`, and set the example
It is the spec's designated place for "needs node / needs pypdf / needs
network", and it is free to read.

* Show it on the panel row; include it in the error when a script fails on a
  missing interpreter or package — the author already told us.
* Declare it on adk-cc's own skills that need something (`web-smoke-check`:
  node + jsdom). 24 built-ins, 0 declarations today.

This also settles **#94**: with 0 of 41 skills declaring anything, a "read the
declaration and install it" feature would build on an empty field. Lazy install
stays keyed to what actually fails, with `compatibility` as a hint when present.

### R3. Trust-gate project skills
The guide's security note, and adk-cc is exposed: opening a cloned repository
loads `<project>/.adk-cc/skills` automatically, so the repo can inject
instructions into the agent's context. The desktop shell already has a
project-add flow where a trust decision fits, and skills are already
enable/disable-able per scope — this is a gate on an existing switch, not a new
subsystem.

### R4. Offered vs used, per skill
Tier B puts the burden of triggering on the description, and nothing tells an
author theirs is not working. adk-cc holds both halves already (enablement
knows what was offered; the verification signals know what was loaded and which
scripts ran). A quiet counter on the row — *"offered on 40 turns, used on 0"* —
is diagnosis pointed exactly where the standard puts responsibility.

### R5. Make skill activity legible, and dedupe repeat activations
A `load_skill` is currently indistinguishable from any other tool call. A
distinct thread row naming the skill, plus a turn-footer line. While in there,
the guide's dedupe: a second activation of a skill already in context
re-injects the same instructions for nothing.

### R6. `/<skill-name>` to force a skill for the next turn
The guide explicitly expects user-explicit activation ("the harness handles the
lookup and injection"). Our slash menu and per-turn enablement filtering both
exist, so this is a command kind plus a "required skill" hint.

### Decide, don't build
* **`allowed-tools`** — honouring it means frontmatter pre-approving tool use
  inside a turn, which touches the permission engine and the protected-path
  floor. Zero of 41 skills use it; the spec calls it experimental. Disclose it
  in R1's warnings now, decide later.
* **`.agents/skills/` interop path** — the guide's cross-client convention.
  Scanning it would make skills installed by other agents visible. Worth doing,
  but it is a scope decision like the earlier `.claude/skills` removal, so it
  belongs to you rather than to me.

## 4. Explicitly not doing

* **Enforcing Tier B.** Body-length and token recommendations are advice to
  authors; two published skills exceed them and work fine. Report, never trim
  to them.
* **Silent catalogue truncation.** The ecosystem's worst skills bug is a budget
  that drops skills without saying so. At ~66 tokens/skill against the guide's
  50–100, there is nothing to solve — and if there ever is, it must name what
  it dropped.
* **A description rewriter.** It edits a vendored file; R4's counter gives the
  author the same information without touching their skill.

## 5. Order

R1 → R2 → R3 → R5 → R4 → R6.

R1 is conformance in the direction of *accepting more*, which is the cheapest
user-visible win and subsumes the malformed-skill detection you asked for. R2
is small and makes our own skills exemplary. R3 is a security gap the guide
names outright. R5 unlocks R4's measurement. R6 is self-contained and can jump
the queue if explicit invocation is wanted sooner.

## Sources

Every normative claim above comes from the open standard (agentskills.io). The
Anthropic pages are used only to cross-check the numbers and to source the
vendor-specific column; the blog posts are used only for complaint themes, not
for any rule. Not consulted: the `skills-ref` reference validator on GitHub —
worth reading before implementing R1, since it is the standard's own idea of
which checks are errors and which are warnings.

- [Agent Skills specification](https://agentskills.io/specification) — Tier A constraints, Tier B recommendations
- [How to add skills support to your agent](https://agentskills.io/client-implementation/adding-skills-support) — Tier C client behaviour: lenient validation, malformed-YAML retry, compaction exemption, trust gating, `.agents/skills/` interop
- [Agent Skills overview](https://agentskills.io) — progressive disclosure, directory layout
- [Agent Skills overview (Anthropic platform docs)](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview) — independent statement of the same limits; Skills-API-only name/description rules; "use Skills only from trusted sources" (second source for R3)
- [Extend Claude with skills](https://code.claude.com/docs/en/skills) — vendor extensions (`user-invocable`, `disable-model-invocation`, `paths`, `skillOverrides`) and the 1,536-char listing truncation
- [Claude Skills: The Controllability Problem](https://paddo.dev/blog/claude-skills-controllability-problem/) — the control/visibility gaps behind R5/R6
- [Claude Code skills not triggering? It might not see them.](https://blog.fsck.com/2025/12/17/claude-code-skills-not-triggering/) — the invisible-budget failure adk-cc must not reproduce
