# Dependency & license risk

## License classes to flag

| Class | Examples | Why it matters |
|---|---|---|
| Strong copyleft | GPL-2.0/3.0, AGPL-3.0 | Distribution/network use can force source disclosure. AGPL in a SaaS product is a frequent deal-blocker. |
| Weak copyleft | LGPL, MPL-2.0, EPL | Usually fine when dynamically linked/unmodified; document the linkage. |
| Permissive | MIT, BSD, Apache-2.0 | Low risk. Apache-2.0 adds a patent grant (a plus). |
| **Source-available (NOT open source)** | BUSL, Elastic, SSPL, Commons Clause | Look open, restrict commercial use. Commonly missed — check the LICENSE text, never the label. |
| Unlicensed / missing | no LICENSE file | Default is "all rights reserved" — legally the worst case, not the most permissive. |

## Checks worth running

```bash
# Python
uv pip list --format=json          # or: pip list
pip-audit                          # known vulns
# Node
npm ls --all --json | head -50
npm audit --json
# Anything
osv-scanner --recursive .
```

Also inspect by hand:
- git/URL dependencies and forks — no upstream security response, no releases.
- Unpinned ranges (`^`, `*`, `>=` with no ceiling) in anything shipped.
- Transitive depth: a 12-package direct list hiding 900 transitive packages is a
  materially different risk profile — report both numbers.
- Single-maintainer or dormant packages on critical paths.

## Reporting rules

- Vulnerabilities: severity **and reachability**. Note when you could not
  determine reachability rather than implying exploitability.
- Secrets: report **that** a credential is committed and **where**; never
  reproduce the value, and recommend rotation — a committed secret is
  compromised even after deletion, because history retains it.
