# 09 — Building and testing

Read in: **English** · [한국어](./09-building.ko.md)

How to build each artifact this repo produces, how to verify a build actually
worked, and the build-time traps that have cost real debugging time here. If you
only want to *run* adk-cc, the README's Quick start is enough — this is for
changing it.

## What gets built

| Artifact | Command | Output |
|---|---|---|
| Python env | `uv sync` | `.venv/` |
| Web UI bundle | `npm --prefix web run build` | `web/dist/` |
| Desktop UI bundle | `npm --prefix web run build:desktop` | `web/dist-desktop/` |
| Desktop app | `cargo build --manifest-path src-tauri/Cargo.toml` | Tauri binary + sidecar |
| Python wheel | `uv build` | `dist/*.whl` |

The two UI bundles are **separate builds of the same source**, differing only by
`VITE_ADK_CC_DESKTOP=1`. Changing shared UI code and rebuilding only one of them
is the most common way to test a stale bundle.

Which one gets served decides which SHELL the user sees, and that tripped real
users: the backend's `ADK_CC_DESKTOP=1` is a runtime switch (desktop routes,
local artifacts, project registry) while the shell is baked into the bundle at
build time, so a desktop-mode backend used to serve the WEB app — no projects
rail, no file tree, no explanation. `ADK_CC_UI_DIST` now defaults to
`web/dist-desktop` when desktop mode is on and that build exists, and logs a
warning naming `build:desktop` when it does not. See
[08-desktop-app.md](./08-desktop-app.md#which-ui-am-i-looking-at).

## Prerequisites

- **uv** — supplies both the interpreter and the packages. The agent's analysis
  runtime also shells out to `uv` at runtime (see below), so it must be on PATH
  for the machine *running* adk-cc, not only for the machine building it.
- **Node 20+** for the UI bundles.
- **Rust + Tauri prerequisites** only if you build the desktop app.

## Build-time configuration lives in the REPO-ROOT `.env`

`web/vite.config.ts` sets `envDir` to the repository root, so `VITE_*` variables
are read from `<repo>/.env` — **not** from `web/.env`.

This is worth stating loudly because it fails silently and misleadingly. During
this project a grep of `web/` reported that HTML-preview scripts were disabled,
while the running app had them enabled, because the flag lives in the root
`.env`. Anything that depends on a `VITE_*` flag can only be settled by looking
at the built bundle or the running DOM.

```bash
# affects the BUILD, not the runtime — a change here needs a rebuild
VITE_ADK_CC_HTML_PREVIEW_ALLOW_SCRIPTS=1   # interactive charts render in previews
```

## Verifying a build

**Use the build command, not `tsc --noEmit`.** `npm run build:desktop` runs
`tsc -b` (project references); `npx tsc --noEmit -p tsconfig.json` uses a
different config and has twice here reported a clean tree for code that would
not compile. Both times a browser test then ran against the *previous* bundle
and reported green for code that was never built.

```bash
npm --prefix web run build:desktop 2>&1 | grep -i "error TS"   # empty = good
```

The same applies after any UI change you intend to test in a browser: build
first, confirm no `error TS`, then run the test. A passing UI test proves
nothing about a bundle that failed to build.

## Packaging: skills ship as package data

`[tool.setuptools.packages.find]` collects `*.py` only. The built-in skills are
Markdown, so `pyproject.toml` declares them explicitly:

```toml
[tool.setuptools.package-data]
adk_cc = ["skills/**/*"]
```

Without that, an installed wheel has zero skills while the repo checkout has all
of them — invisible in development, broken for every user. `tests/test_builtin_skills.py`
builds a real wheel and asserts the SKILL.md files are inside it.

## The analysis runtime is provisioned at RUN time

`uv` also supplies the agent's data-analysis environment
(`.adk-cc/analysis-env/` inside each workspace, created on first use, ~20-60s).
It is not part of any build step, and it is per project, so a fresh clone or a
new project pays for it once. The UI shows a chip while it happens; the state is
readable without triggering it:

```bash
curl 'localhost:8000/desktop/analysis-env?project_id=<id>&session_id=<sid>'
```

Set `ADK_CC_ANALYSIS_ENV=off` to fall back to bare `python3` (not recommended:
on stock macOS that is Python 3.9 with no data packages), or point it at an
interpreter you control.

## Tests

Every test file is a standalone script — no pytest runner, no collection step:

```bash
.venv/bin/python tests/test_builtin_skills.py       # one file
```

Sweep the whole unit/integration suite (~3 minutes, 121 files):

```bash
for f in tests/test_*.py; do
  .venv/bin/python "$f" >/tmp/out 2>&1 || echo "FAIL $f: $(tail -1 /tmp/out)"
done
```

### Three classes of test

- `tests/test_*.py` — unit + integration. No model, no network. These must pass.
- `tests/e2e_*.py` — real server, usually a real browser (Playwright). Most are
  model-free and skip cleanly when a prerequisite is missing (`web/dist-desktop`
  absent, Playwright not installed).
- `ADK_CC_LIVE=1 tests/e2e_*.py` — the subset that spends a real model turn.
  Opt-in. Pacing depends on the ENDPOINT: the ChatGPT-subscription path these
  tests pin (`chatgpt-codex/gpt-5.4-mini`) has no practical limit, so repeat
  runs are fine; API-key endpoints do, and want `ADK_CC_MODEL_MAX_RPM` rather
  than ad-hoc sleeps.

  Run them repeatedly when a UI assertion depends on how the agent happens to
  behave. The run-view test passed, failed, then passed on identical code —
  because the model sometimes emits four artifacts in ONE event and sometimes
  in four, and only the second shape exercised the grouping path. One green run
  would have shipped the bug.

### The env trap that has bitten every live test here

The repo `.env` configures a real deployment — including
`ADK_CC_SANDBOX_BACKEND=daytona`. Tests that do not opt out inherit it and fail
in confusing ways (`daytona: backend used before ensure_workspace()`), while
tests that DO opt out (`ADK_CC_SKIP_DOTENV=1`) lose the real model config and
every live turn dies with an authentication or connection error.

So:

```bash
# model-free test: ignore the deployment config
ADK_CC_SKIP_DOTENV=1 ADK_CC_SANDBOX_BACKEND=noop .venv/bin/python tests/test_read_file_limits.py

# LIVE test: keep the real config, drop the stubs
env -u ADK_CC_API_KEY -u ADK_CC_SKIP_DOTENV ADK_CC_LIVE=1 \
  .venv/bin/python tests/e2e_markdown_table_ui.py
```

A live test that boots its server with `ADK_CC_API_KEY=stub` will fail with
`Connection error` on every turn — the harness, not the product.

### Adding an env var

`agents/adk_cc/config/schema.py` is the single source of truth. Reading an env
var that is not declared there fails `tests/test_config_schema.py`, and the
committed `.env.example` is generated from the schema:

```bash
.venv/bin/python -m adk_cc.config gen-env --out .env.example
```

Before adding one, check whether an existing var already means the same thing —
the test catches unregistered vars, not synonyms, and a second name for one idea
has to be kept in sync forever.

## Known-failing tests

A handful of suite failures predate current work and are environment-dependent
rather than broken behaviour (`test_admin_panel`, `test_grant_flow`,
`test_session_title_plugin`, `test_daytona_backend`,
`test_sandbox_service_backend`, `test_workspace_extra_roots`,
`test_working_dirs_persist`). When judging whether a change broke something,
compare against the same tests on a baseline commit rather than assuming a red
result is yours:

```bash
git worktree add -q --detach /tmp/baseline <commit-before-your-work>
(cd /tmp/baseline && /path/to/.venv/bin/python tests/test_x.py)
```
