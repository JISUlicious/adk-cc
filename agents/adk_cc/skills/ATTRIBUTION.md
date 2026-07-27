# Built-in skill attribution

Each built-in skill records its upstream source, license, and the commit it was
vendored from. Skills authored for adk-cc are marked `first-party`.

| Skill | Upstream | License | Vendored |
|---|---|---|---|
| `data-analyst` | [JISUlicious/pd-skills](https://github.com/JISUlicious/pd-skills) — `.claude/skills/data-analyst` | first-party | 2026-07-27 |
| `tech-due-diligence` | authored for adk-cc | first-party | 2026-07-27 |
| `feasibility-study` | authored for adk-cc | first-party | 2026-07-27 |
| `prd-writer` | authored for adk-cc | first-party | 2026-07-27 |
| `contract-review` | authored for adk-cc (informed by w95 `contract-review`, MIT) | first-party | 2026-07-27 |
| `nda-triage` | authored for adk-cc (informed by w95 `nda-triage`, MIT) | first-party | 2026-07-27 |

The three R&D skills are **authored, not adopted**: the 1,185-skill corpus
surveyed in `analysis/skills-program.md` yielded only 4 clean candidates in this
domain — the thinnest of any — while it is precisely where adk-cc's ability to
read a real codebase is an advantage no generic office-skill pack has.

Local edits on vendoring: companion `.md` files moved to `references/` so they
load at discovery as well as on demand; an "adk-cc runtime" section added
describing the uv-managed analysis env and the artifact convention. The
methodology itself is unmodified.

The Korean variant (`data-analyst-ko`) is intentionally NOT built in — a second
near-identical catalog entry costs skill-selection precision. Install it as a
project skill or via `ADK_CC_SKILLS_DIR`.
