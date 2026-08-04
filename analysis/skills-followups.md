# #96 and #94 in detail

Written 2026-07-31, after the live multi-skill runs. #95 is now implemented
(commit `2cee974`); these two are the open ones.

---

# #96 — a behaviour claim about an earlier turn's artifact escapes the nudge

## What happened, exactly

Same four-turn scenario, three runs. Turn 2 built `index.html`; turn 3 asked
*"confirm the colour control actually changes what a visitor sees"*.

| Run | Turn 2 built | Turn 3 answer | Tool calls in turn 3 | Nudge |
|---|---|---|---|---|
| 1 | CSS-3D lamp | drove the page with `web-smoke-check` | yes | — |
| 2 | CSS-3D lamp | *"Yes — the colour control does change the page's visible lamp rendering."* | **none** | silent |
| 3 | Three.js lamp | *"Yes — the colour control is wired to change the 3D preview, not just the labels."* | **none** | silent |

`grep -c 'VERIFICATION CHECK'` across all three sessions: **0**.

Then I drove both pages in real Chromium:

* Run 2's page: the claim was **true**. Sand → Sage changes shade, glow and
  caption in rendered pixels.
* Run 3's page: the claim was **false**. Zero `<canvas>` elements and a console
  error — `Failed to resolve module specifier "three"`. Three.js never loads;
  the preview area is an empty gradient. The agent had also reported
  "drag-to-rotate controls via OrbitControls" in turn 2.

Same prompt, same absence of verification; the difference between a true claim
and a shipped falsehood was luck.

## Why the signal cannot fire

`verification/signals.py:191`:

```python
unexercised_page = claims AND built_a_page AND NOT page_was_driven AND NOT hedged
```

`built_a_page` is derived from what *this turn* did. The page came from the
previous turn, so the property is False and the conjunction can never be true —
regardless of how confident the claim is. The nudge covers *"I built it and it
works"* and misses *"yes, it works"*, and the second is the more natural way a
person asks. A user who splits build and check across two messages — the
obvious, careful way to work — gets no verification pressure at all.

Turn 2 is a second, milder case: it built the page AND claimed behaviour
("preview updates when shade changes") without driving it. There the signal
*could* fire; it did not, which is worth a separate look at `claims` and
`hedged` extraction on a long, bulleted "Done — …" answer.

## What to change

The fix is not a bigger regex. It is that verification state must outlive a
turn:

1. **Remember the artifact.** When a turn writes something drivable (`.html`,
   a served app), record `{path, digest, last_driven_turn}` in session state.
   Small, and it already has a natural home next to `temp:skills_loaded`.
2. **Score the claim against that record**, not against what this turn built:
   a behaviour claim naming a known artifact that has not been driven *since
   its last modification* fires the nudge. This also catches the sneakier
   case — page driven in turn 2, edited in turn 4, claimed again in turn 5.
3. **Keep it turn-local for evidence.** Driving the page in an earlier turn
   only counts while the artifact has not changed since; a digest makes that
   check exact rather than a guess about staleness.

`skipped_a_shipped_script` already reasons across a turn boundary in spirit,
so the shape is not new to this module.

## Sizing

Small: one state key, one property, one nudge branch. The test is the
interesting part — it needs a two-turn fixture (build, then claim in a fresh
turn), which the existing signal tests do not have.

---

# #94 — what "dependencies" actually means for third-party skills

Measured across the 17 published example-skills plus the third-party
`openscad` skill. Counts are occurrences across `SKILL.md` and `scripts/`.

## The five kinds, with real examples

### 1. Python packages — the common case, and the one we can install
* `pypdf` — 5 imports, the `pdf` skill (**this is what failed live**:
  `ModuleNotFoundError: No module named 'pypdf'`)
* `defusedxml` — 3, shared by `docx` / `pptx` / `xlsx`
* `PIL`/pillow, `imageio`, `imageio-ffmpeg`, `numpy` — `slack-gif-creator`
* `mcp`, `anthropic` — `mcp-builder`
* `pandas`, `numpy`, `scipy` — our `data-analyst`

adk-cc already provisions Python by tier (`core` / `modeling` / `stats`).
None of pypdf, defusedxml or pillow is in any tier, which is exactly why the
published document skills fail.

### 2. Node runtime and npm packages
* `node` — **149** occurrences; `pnpm` 16, `npm` 11, `npx` 1
* `web-artifacts-builder` runs `pnpm create vite` and installs a shadcn
  component set — a network install of hundreds of packages
* our `web-smoke-check` needs `jsdom`, installed into a shared
  `~/.adk-cc/web-runtime`

### 3. System binaries — the kind we cannot install
* `soffice` / `libreoffice` — 32, the document skills' conversion path
* `convert` (ImageMagick) — 11
* `qpdf` — 8, `gs` (Ghostscript) — 6, `pandoc` — 3
* `openscad` — the third-party skill, which hardcodes
  `/opt/homebrew/bin/openscad`

There is no portable, safe way to install these on a user's machine. The honest
handling is detection and a clear message — which R2 now does, quoting the
skill's own `compatibility` when it has one.

### 4. Network access
* `curl` — 12 occurrences; plus every npm/pnpm install
* This is a permission question, not a provisioning one.

### 5. Credentials
* `mcp-builder` imports `anthropic` — needs an API key
* Already solved: per-user skill secrets, declared via
  `metadata["x-adk-cc/secrets"]`.

## What skills actually declare

| Signal | Coverage |
|---|---|
| `compatibility` frontmatter (the spec's designated field) | **0 of 41 skills** |
| shipped `requirements.txt` | **2 of 17** — `slack-gif-creator` (pillow, imageio, imageio-ffmpeg, numpy), `mcp-builder/scripts/` (anthropic, mcp) |
| `package.json` | 0 |
| imports parseable from shipped `.py` | all of them |

This is the finding that shapes the design: **a feature that reads a
declaration would sit idle on 39 of 41 skills.** The information exists almost
entirely in the code, not in the metadata.

## What a lazy install should therefore be

1. **On first run of a skill's script** (the marker already exists —
   `.adk-cc/skill-runtime/<skill>/<digest>/`), collect requirements from, in
   order: a shipped `requirements.txt`, then `compatibility`, then top-level
   imports of the script and its siblings that are not stdlib and not
   already importable.
2. **Install only Python packages, only into the session's analysis env.**
   Never into the user's system Python; never a system package manager.
3. **Ask before the first install for a given skill version**, naming the
   packages — this is `pip install` of third-party code, which deserves the
   same treatment #95 just gave the scripts themselves. It should reuse that
   same prompt, and can piggyback on the same click: *"run this script, and
   install pypdf, defusedxml"*.
4. **Never infer from a failure.** Reading `ModuleNotFoundError: No module
   named 'x'` and installing `x` is how a typo becomes a supply-chain
   incident (`reqeusts`, `python-dateutil` vs `dateutil`). The current
   behaviour — name it, tell the agent to report the step as NOT RUN — stays
   the fallback.
5. **Node: reuse the web-runtime pattern** — a shared, per-package-set
   directory outside the project, installed on demand, never into the user's
   project.
6. **System binaries: never install.** Detect, name, quote `compatibility`,
   and let the agent report the step as not run. Already shipped.

## Sizing

The provisioning path (1, 2, 5) is a day; the interesting cost is the
permission design in (3), which should not be invented separately from the
gate that just landed for #95.
