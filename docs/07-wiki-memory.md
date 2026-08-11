# Running with the wiki and memory

Three scopes, each with its own home (#127 completed the set):

- **Session → notes** — a curated per-session document in session state:
  the agent records decisions/constraints/task state via
  `update_session_notes`, the block re-injects into every request (so it
  survives context compaction), `/notes` shows it, and it dies with the
  session. `mode=promote` lifts a note into user memory. Knobs:
  `ADK_CC_SESSION_NOTES_BUDGET` (2000 tokens),
  `ADK_CC_SESSION_NOTES_AUTOCAPTURE` (=1, web: capture routes
  session-scoped facts into notes instead of dropping them; default off).

Two related but deliberately separate subsystems for the other scopes:

- **Wiki** — an *explicit*, shared domain-knowledge base. The agent gets
  three tools (`wiki_search`, `wiki_read`, `wiki_add`); `wiki_add` writes
  only to the calling user's **inbox**, and an offline **librarian** pass is
  the single writer that merges inbox notes into the shared `domain/` pages
  (conflict policy: auto-supersede; contradictions need `corroboration_n`
  independent users + an external source, else quarantine). Nothing is
  injected automatically — the model must search.
- **Memory** — an *autonomous* per-user memory. No tools: a plugin recalls
  a budgeted block into every turn's system instruction and captures facts
  after each turn (LLM extract → verified identity-resolve → episodic
  store), with episodic→semantic consolidation on a scheduler or cron.

## 1. Is it on by default?

| Mode | Default |
|---|---|
| **Desktop app** | **ON — hard-wired.** The launcher sets `ADK_CC_WIKI=1`, `ADK_CC_MEMORY=1`, `ADK_CC_KNOWLEDGE_UI=1` with roots under the app data dir (`src-tauri/src/main.rs`). Scoping: memory and wiki-inbox are **per-project** (the project id is the user id); `domain/` wiki is shared across the machine's projects. Opt out only by editing `~/.adk-cc-desktop/settings.env`. |
| **Web / server** | **OFF — everything.** The schema defaults for `ADK_CC_WIKI`, `ADK_CC_MEMORY`, `ADK_CC_KNOWLEDGE_UI` are all false and no packaging enables them. An operator turns them on per the next section. |

## 2. Enabling on a web deployment

Minimum:

```bash
ADK_CC_WIKI=1
ADK_CC_WIKI_ROOT=/srv/adk-cc/wiki          # default: <workspace>/.wiki
ADK_CC_MEMORY=1
ADK_CC_MEMORY_ROOT=/srv/adk-cc/memory      # default: <workspace>/.memory
ADK_CC_KNOWLEDGE_UI=1                      # the /knowledge graph view
```

**Security floor for authenticated web (read this):**

- Identity scoping follows the **auth principal** (fix `79cada2`): a
  client-supplied `userId` that differs from the principal is ignored for
  storage scoping and rejected with 403 at `/api/turns`. Still set
  **`ADK_CC_AUTHZ=1`** so the ADK-native `/apps/*` paths enforce ownership
  too — the server logs a warning at boot if memory/wiki are on without it.
- **Single-tenancy shares one `domain/` wiki across every user of the
  deployment.** That is the "team wiki" design — make it a conscious
  choice. `ADK_CC_TENANCY_MODE=multi` partitions everything per tenant.
  Memory and wiki inboxes are always per-user.

**Consolidation** (memory does little without it) — either the in-process
scheduler (single-worker deployments only):

```bash
ADK_CC_MEMORY_CONSOLIDATE_INTERVAL_S=900   # every 15 min; 0/unset = off
# or event-driven: consolidate a topic once it has N fresh episodics
ADK_CC_MEMORY_CONSOLIDATE_THRESHOLD=2
```

…or cron on multi-worker deployments: `scripts/memory_consolidator.py`
hourly. The wiki librarian is **cron-only**: `scripts/wiki_librarian.py`
(suggested `*/15 * * * *`) — without it, inbox notes never reach the
shared pages.

**Knobs that matter** (full list: `.env.example`, group "Memory & Wiki"):

- `ADK_CC_MEMORY_RECALL_BUDGET_TOKENS` (600) — size of the per-turn recall
  block.
- `ADK_CC_MEMORY_AUTOCAPTURE` (1) — the post-turn capture costs one hidden
  model call (~1.5–1.8k tokens) per turn; set 0 to disable capture while
  keeping recall.
- `ADK_CC_MEMORY_SYNTH` (llm) — `deterministic` for model-free
  consolidation (latest-wins).
- `ADK_CC_WIKI_CORROBORATION_N` (2) — also adjustable live per tenant via
  the admin API (`/wiki-settings/corroboration_n`).
- `ADK_CC_COMPACTION_SEED_MEMORY=1` — seed context-compaction summaries
  with durable user facts (needs compaction enabled).

**Costs to expect with memory on:** +600 tokens on every request (recall
block) and one ~1.5–1.8k-token capture call after every turn, off the
user-visible path.

## 3. On-disk layout

```
<WIKI_ROOT>/<tenant>/domain/wiki/<slug>.md        librarian only (shared pages)
<WIKI_ROOT>/<tenant>/users/<uid>/inbox/…          wiki_add, turn-time
<WIKI_ROOT>/<tenant>/users/<uid>/merged/…         archived after publish
<MEMORY_ROOT>/<tenant>/users/<uid>/episodic/…     capture, turn-time
<MEMORY_ROOT>/<tenant>/users/<uid>/semantic/…     consolidation only
```

Plain files + `_kv/` metadata; only `file://` storage is implemented.
Memory deliberately **persists across a user's sessions** (workspaces do
not, since the per-session isolation change) — that persistence is the
point of memory.

## 4. The /knowledge view

`/knowledge` (desktop rail link, or the `/wiki` slash command in either
shell) renders two graph tabs: **wiki** (green domain pages + your own
inbox notes, edges from `[[wikilinks]]`) and **memory** (violet semantic
facts + gray episodics feeding them). Click a node for its content. The
view is read-only and works in BOTH shells; the backend routes exist only
when `ADK_CC_KNOWLEDGE_UI=1` — when the server has it off, the page shows
an explanation instead of a broken graph. The graph endpoints always
scope to the **authenticated** user; there is no cross-user view.

## 5. Verifying it works

1. Boot with the flags on; say something durable ("나는 pandas 2.x만
   쓴다"); end the turn. `ls <MEMORY_ROOT>/<tenant>/users/<uid>/episodic/`
   → a new file.
2. Next turn, ask "what do you know about me?" — the recall block should
   surface it (or check the `# Memory` block via model IO trace).
3. Run the consolidator (or wait for the scheduler); the fact moves to
   `semantic/`, visible on the `/knowledge` memory tab.
4. Ask the agent to `wiki_add` a domain fact; it appears in your inbox on
   the wiki tab; run `scripts/wiki_librarian.py`; it merges into `domain/`.
