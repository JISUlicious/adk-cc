# Implementation plan: what's left, filtered by need

2026-07-31. Every open candidate evaluated first; only what survives gets a
phase. The filter: measured harm beats convenience, an existing mitigation
lowers the need, and anything whose trigger was never observed live is out.

## 1. Needs evaluation

| Candidate | Evidence of need | Mitigation today | Verdict |
|---|---|---|---|
| #96 cross-turn behaviour claims | **Shipped falsehood, measured**: run 3 confirmed a 3D preview that never loads; nudge structurally blind (`built_a_page` is turn-scoped) | none | **BUILD (P1)** |
| #96b same-turn miss | Turn 2 in runs 2–3 built the page AND claimed behaviour, undriven — the existing signal *should* have fired and stayed silent | signal exists, silent | **INVESTIGATE first** — likely a claims/hedge-extraction bug; may shrink P1 |
| #95 end-to-end proof | Gate shipped with 15 unit tests; never seen live through ADK's confirmation machinery and the UI card | unit tests | **VALIDATE (P0)** — cheap, and it guards a security fix |
| #94 Python packages | **Live failure**: `pypdf` (pdf skill); `defusedxml` blocks docx/pptx/xlsx — the flagship published skills | named error + "report NOT RUN" stopgap | **BUILD, scoped (P2)** |
| #94 Node / npm | `web-artifacts-builder` worked live (node present, pnpm fetched); jsdom already has the shared web-runtime | works | **SKIP** |
| #94 system binaries (soffice, ImageMagick, qpdf, gs, openscad) | can't be installed safely on a user's machine | R2: detected, named, `compatibility` quoted | **SKIP — done** |
| #94 network access | curl etc. | bash gate + #95 gate ask | **SKIP** |
| #94 credentials | anthropic key etc. | `x-adk-cc/secrets` per-user store | **SKIP — done** |
| `test_bash_background_timeout` flake | fails ~1 in 3 sweeps (analysis-env cold start inside the timed section) — pollutes every sweep signal | none | **TINY FIX** — fold into P1's commit |
| #65 stale tests, #75 remote datasets, `allowed-tools`, `.claude/skills` alias, marketplace | no new evidence this session | parked by decision | **STAY PARKED** |

Two conscious limitations to record, not fix now:
* An `Allow always` grant on `<skill>:*` is name-keyed, so a later *edit* to a
  trusted skill's script runs without a re-ask. `run_bash` grants have the same
  property (command-string-keyed). Parity is acceptable; a digest-keyed grant
  would be the upgrade if it ever bites.
* The dedupe/counters treat a session as one context; nothing expires grants.

## 2. P0 — prove #95 live (~half day)

The gate is unit-tested at the plugin boundary; ADK's two-call confirmation
machinery and the UI card are not exercised. One live pass:

1. Re-run `scripts/acceptance_multiskill.py`. The openscad turn must now
   produce a confirmation for `run_skill_script` *before* the script runs —
   nothing should appear in `~/openscad-projects` until it is answered.
2. Verify the harness's `_pending_confirmations` matches the gate's card. It
   currently matches only `adk_cc_confirmation_form`; if the skill gate
   surfaces differently, fix the harness (or the surfacing) so unattended runs
   don't silently stall — the exact failure mode this harness had once already.
3. One UI screenshot of the card: the script source must render legibly
   (`whitespace-pre-wrap` should carry it; confirm, don't assume).
4. Assert the approvals log names the skill script.

Exit criterion: a live run where the script runs only after an answer, and
`~/openscad-projects` exists only if the answer was allow. Clean up after.

## 3. P1 — #96, verification across turns (~1 day)

**Step 0 — measure before building.** Feed run 2's actual turn-2 answer
("Done — `index.html` is in place … preview updates when shade changes") into
`collect()` and see why `unexercised_page` stayed silent in the same turn.
If claims-extraction misses this phrasing, fix that first — it is cheap and
narrows what the cross-turn work must carry.

**Step 1 — remember drivable artifacts.** Session-state key
`adk_cc_artifacts`: `{path: {digest, driven_since_change: bool}}`.
* Written by the nudge plugin when a turn's events show `write_file`/`edit_file`
  of a drivable file (`.html`, plus whatever `built_a_page` already matches).
  Digest from the written content (already in the tool args — no fs read).
* `driven_since_change` set true when that turn's signals show the page was
  driven (web-smoke-check on that path, or the existing `page_was_driven`
  heuristics); reset to false on any later write to the same path. The digest
  makes "since change" exact, covering: driven turn 2, edited turn 4, claimed
  turn 5.

**Step 2 — the signal.** New property on `TurnSignals`
(`stale_behaviour_claim`): claims present, not hedged, no evidence this turn,
and the session registry holds an artifact with `driven_since_change=False`.
The registry is passed into `collect()` by the plugin — signals stays a pure
function, testable without a session.

**Step 3 — nudge text.** Same shape as the existing branches: name the
artifact, say it has not been exercised since it last changed, point at
`web-smoke-check`, accept an honest hedge as a way out.

**Step 4 — tests.**
* Unit: a two-turn fixture (build in turn 1, bare "yes it works" in turn 2) —
  the case no existing signal test has.
* The edit-invalidates-drive case.
* Honest hedge and actually-driving both silence it.
* Live: re-run the multiskill scenario; exit criterion is
  `VERIFICATION CHECK ≥ 1` in any run whose turn 3 makes the claim without
  driving — measured 2-of-3 before, so one run is likely enough.

Include here: the `test_bash_background_timeout` warm-up fix (move
`ensure_env` ahead of the timed section).

## 4. P2 — #94, Python-only lazy install (~1 day)

Scope pinned by the measurements: Python packages only, into the session's
analysis env only, asked about through the gate that already exists.

**Step 1 — collect requirements per skill** (pure function, testable):
priority order `scripts/requirements.txt` (2 of 17 skills) → `compatibility`
text (0 today, but it is the spec's field) → top-level imports of the target
script and its `.py` siblings, minus `sys.stdlib_module_names`, minus what the
analysis env already imports. Import→distribution mapping table for the
measured cases only (`PIL→pillow`, `yaml→pyyaml`, `cv2→opencv-python`); an
unmapped name passes through as itself. Everything is shown to the user, so a
wrong guess is visible before it installs.

**Step 2 — ask through the #95 gate, one click, not two.** When the gate fires
for a script whose requirements are not yet satisfied, append to the card:
`will also install into the analysis environment: pypdf, defusedxml (from
imports)`. Allow once/always then covers both the run and the install. No new
prompt type, no second interruption. If the gate is already granted
(`skill:*`) but deps are missing, ask once with the same card — an install is
a side effect the user has not yet seen.

**Step 3 — install.** In the launcher, before exec: `uv pip install` the list
into the session analysis env; write `.deps-ok` beside the existing `.ready`
marker in `.adk-cc/skill-runtime/<skill>/<digest>/` so it runs once per skill
version. Install failure: report which packages failed, run the script anyway
(it may not need them all), never retry silently.

**Hard rules, restated from the deep dive:** never infer a package from a
`ModuleNotFoundError` at runtime (typo-squatting); never touch system Python
or a system package manager; never attempt binaries. The existing named-error
stopgap remains the fallback for everything outside this scope.

**Tests.** Unit: the collector against fixture skills (manifest, imports-only,
already-satisfied); the gate card naming the packages; marker semantics.
Live acceptance: the published `pdf` skill's `extract_form_field_info.py`
succeeding end-to-end — the exact script that failed with
`No module named 'pypdf'`.

## 5. Order and cost

P0 (½ day) → P1 (1 day) → P2 (1 day). P0 first because it validates shipped
security work; P1 before P2 because proven harm outranks convenience.

## 6. Explicitly not in this plan

Node/npm provisioning, system binaries, network-access mediation beyond the
existing gates, `allowed-tools`, grant expiry, digest-keyed grants, #65's
stale tests, #75. Each is either mitigated, unobserved live, or parked by
decision — reopen on evidence, not on symmetry.
