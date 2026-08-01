# Branding: where the name lives, and what a rename actually touches

2026-08-01. Measured, not guessed: `adk_cc`/`ADK_CC` — 2,123 occurrences in
176 files; `adk-cc` — 599 in 144; `adkcc` — 101 in 37. That number looks
frightening and mostly is not: the occurrences fall into three layers with
completely different costs, and only the smallest layer is *brand*.

## Layer A — the brand surface (what a user ever sees)

Small, concentrated, and safe to change today:

| Surface | Where |
|---|---|
| Browser tab title | `web/index.html:7` `<title>adk-cc</title>` |
| Wordmark in both rails | `web/src/shells/desktop/ProjectRail.tsx:263`, `web/src/shared/components/SessionRail.tsx:170` — the `<span>adk-cc</span>` next to the logo |
| Sign-in card | `web/src/shells/web/AuthGate.tsx` — "Sign in to adk-cc", "Create your adk-cc account", "Request access…", the cannot-reach-server error (5 strings) |
| Settings copy | `DesktopSettingsSections.tsx:369` "ship with adk-cc" |
| Thread copy | `Thread.tsx` ~205 ("the adk-cc agent on the server") |
| Desktop app name + window title | `src-tauri/tauri.conf.json`: `productName`, `app.windows[].title` |
| **Model-facing self-name** | `prompts.py` (a handful of "adk-cc" mentions), the `[adk-cc]` prefix on injected tool-result notes (bash hint, skills errors, dep notes), and the verification label "⚠ adk-cc:" — these teach the agent what to call itself, and they surface in cards the user reads |
| Dist name | `pyproject.toml` `name = "adk-cc"` |
| Docs | `README.md` (43 hits), `docs/*.md` + Korean twins |

## Layer B — load-bearing identifiers (rename = migration)

These *contain* the name but function as protocol, paths, or stored data.
Each is changeable, none is free:

| Identifier | Role | Cost of renaming |
|---|---|---|
| `ADK_CC_*` env prefix | 275 vars, but **centralised** in `config/schema.py` (single source for parse + gen-env + validation) | Mechanical rename + a dual-read shim (accept old prefix with a deprecation warning) — the schema being central makes this the cheapest B item |
| `agents/adk_cc/` package dir | ADK's app discovery derives the **API app name** from this folder → `/apps/adk_cc/...` and `app_name` stamped into every stored session | `git mv` + import sweep is mechanical, but existing sessions/DB rows are keyed to the old app name; needs a read-both or migrate step. The web client discovers the app via `/list-apps`, so the frontend mostly follows automatically |
| `~/.adk-cc`, `~/.adk-cc-desktop` | user data roots (`deployment.py`) | rename with fallback-read (try new, fall back to old, or one-shot move) |
| `<project>/.adk-cc/` | per-project skills / config / scratch / skill-runtime (20 call sites) | every existing project carries one; needs old-name acceptance, same shape as the `.claude/skills` decision made earlier |
| `com.adk-cc.desktop` | Tauri bundle identifier — the app's identity to the OS | changing it makes existing installs a *different app* (fresh OS-level state); usually kept for life |
| `adk_cc_confirmation_form`, `adk_cc_pending_confirmation`, `adk_cc_allow_rules`, `adk_cc.*` localStorage keys | internal protocol between server, session history and UI | renaming breaks rendering/resume of **old sessions**; zero user-visible value in changing — recommend never |
| `x-adk-cc/secrets` | SKILL.md metadata convention for per-user skill secrets | keep accepting old key forever; optionally add the new alias |

## Layer C — code-internal spelling

`from adk_cc import …`, `sys.path` inserts in every test, log logger names,
comments. Huge count, purely mechanical, and only worth touching if Layer B's
package rename happens (they're the same change).

## Recommended path: brand as a layer, not a rename

The finding that shapes this: **the UI already learns almost everything
dynamically** (app name via `/list-apps`, catalog via APIs). The brand can be
one variable, not six hundred edits.

### Stage 1 — a brand constant, swap the display surfaces (~half a day)

1. `agents/adk_cc/branding.py`: `BRAND = os.environ.get("ADK_CC_BRAND") or
   "<NewName>"` (+ optional tagline). Registered in the config schema.
2. `web/src/shared/brand.ts`: name from `VITE_BRAND_NAME` with the same
   default; used by title, rails, AuthGate, settings copy. (Or served from a
   tiny `/branding` endpoint so server and UI can never disagree.)
3. Swap every Layer A surface to read it — including the model-facing ones:
   prompts say "You are <Brand>…", the `[adk-cc]` note prefixes and the
   verification label become `[<Brand>]`.
4. Tauri `productName` + window title.
5. README/docs headline pass (full docs sweep can trail).

Nothing breaks: env vars, dirs, API paths, old sessions all untouched. The
env-var override also means the brand can be tried before it is settled.

### Stage 2 — externals, when the name is final

`pyproject` dist name, repo/folder name, full docs sweep, UI localStorage keys
(they just reset preferences).

### Stage 3 — identifiers, only if wanted, one at a time

In cost order: env prefix (cheap, schema is central; ship dual-read) →
data dirs (fallback-read) → project dir (accept both names) → package dir +
API app name (session migration) → Tauri identifier (probably never).

### Never

The internal protocol names (`adk_cc_confirmation_form` and friends): renaming
them buys nothing a user can see and breaks the rendering of every existing
session.

## Constraints on the name itself

Worth knowing before choosing, so Stage 3 stays open:

* lowercase, no spaces → usable as a directory (`.newname/`), an env prefix
  (`NEWNAME_`), and a Python package (`newname` — so no leading digit, no
  hyphen in the package form; hyphen fine for display/dist).
* short helps: it appears in env vars 275 times and in prompts the model reads
  every turn.
* the `x-<name>/` metadata prefix should be unlikely to collide with other
  vendors' skill metadata.
