# Built-in skill attribution

Each built-in skill records its upstream source, license, and the commit it was
vendored from. Skills authored for adk-cc are marked `first-party`.

| Skill | Upstream | License | Vendored |
|---|---|---|---|
| `data-analyst` | [JISUlicious/pd-skills](https://github.com/JISUlicious/pd-skills) — `.claude/skills/data-analyst` | first-party | 2026-07-27 |

Local edits on vendoring: companion `.md` files moved to `references/` so they
load at discovery as well as on demand; an "adk-cc runtime" section added
describing the uv-managed analysis env and the artifact convention. The
methodology itself is unmodified.

The Korean variant (`data-analyst-ko`) is intentionally NOT built in — a second
near-identical catalog entry costs skill-selection precision. Install it as a
project skill or via `ADK_CC_SKILLS_DIR`.
