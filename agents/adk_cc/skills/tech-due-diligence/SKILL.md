---
name: tech-due-diligence
description: >
  Assess a codebase's real state — architecture, quality, security, licences,
  operational and key-person risk — citing command output and file:line. Use
  for acquisition diligence, vendor review, or inheriting a system.
metadata:
  x-adk-cc/verify: |
    {"mode": "self", "checks": ["every finding cites a command output or file:line", "the 'what I could not determine' section is present and honest", "severity is by consequence (deal/months/quarters/hygiene), not by taste", "at least two cited commands were actually run this turn"]}
---

# Technical due diligence

You assess a codebase the way an acquirer's technical advisor would: every claim
carries evidence (a command and its output, a file and line), and the risks are
ranked by what would actually cost money or time.

**Ground everything in the repository.** A diligence report assembled from the
README is worthless — the gap between what a project claims and what its code
does is exactly what you are hired to find.

## Workflow

### 1. Frame it
Ask (or state your assumption): what decision does this inform — acquisition,
investment, vendor selection, or inheriting maintenance? The decision sets the
bar. An acquirer cares about liabilities that transfer; a maintainer cares about
onboarding cost.

### 2. Establish the shape
```bash
git log --oneline | wc -l                  # history depth
git log -1 --format='%ci'                  # last commit — is it alive?
git shortlog -sne --all | head -20         # contributors, concentration
cloc . 2>/dev/null || (git ls-files | wc -l)
```
Record: languages, size, age, release cadence.

### 3. Dependency & license risk *(usually the biggest transferable liability)*
Read the manifests directly (`pyproject.toml`, `package.json`, `go.mod`,
`Cargo.toml`, `requirements*.txt`). Then:
- **Licenses** — flag copyleft (GPL/AGPL/SSPL) in anything distributed, plus
  "source-available" licenses that look open but are not (BUSL, Elastic,
  Commons Clause). This is the single most common deal-blocking finding.
- **Supply chain** — direct vs. transitive count, unpinned versions, forks and
  git-URL dependencies, packages with one maintainer or no releases in 2 years.
- **Known vulnerabilities** — `pip-audit`, `npm audit`, `osv-scanner` if
  available. Report severity **and** whether the vulnerable path is reachable;
  an unreachable CVE is noise.
See `references/dependency-risk.md`.

### 4. Test & CI reality
Do not trust a coverage badge. Count tests, run them, and read what they assert:
```bash
# find and actually RUN the suite; a suite that doesn't run is a finding
ls .github/workflows/ 2>/dev/null && grep -l "test" .github/workflows/*
```
Look for the failure modes in `references/test-quality.md` — mocked-everything
tests, assertion-free tests, disabled/skipped suites, and tests that never fail.

### 5. Architecture & change risk
- Entry points, module boundaries, where state lives, what talks to the network.
- **Churn × complexity**: files changed most often are where defects and cost
  concentrate.
  ```bash
  git log --format=format: --name-only --since=12.months | sort | uniq -c | sort -rg | head -20
  ```
- **Key-person risk**: `git shortlog -sne` concentration; if one author owns
  >50% of commits to critical paths, say so plainly — it is a real cost.
- Dead code, TODO/FIXME/HACK density, commented-out blocks.

### 6. Operational readiness
Secrets handling (grep for committed credentials — report **existence and
location, never the value**), config management, migrations, observability,
build reproducibility, whether a newcomer can run it from a clean checkout.

### 7. Write the report
Use `references/report-template.md`. Rules:
- **Every finding cites evidence** — command + output excerpt, or `file:line`.
- **Severity by consequence**, not by taste: Critical (blocks the deal / legal
  exposure), High (months of work), Medium (quarters of drag), Low (hygiene).
- Separate **facts** from **inference** from **recommendation**. Say "I could
  not determine X" rather than guessing.
- Lead with the three findings that change the decision.

## Anti-patterns

- Reporting line counts and dependency totals as if they were insight.
- Grading style instead of risk (formatting is not diligence).
- Treating every CVE as equal without reachability.
- Silent gaps: if you could not run the tests, that IS the finding — write it
  down rather than omitting the section.
- Copying the project's own claims into your report unverified.
