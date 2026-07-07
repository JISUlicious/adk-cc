# Plan: Knowledge graph visualizer (wiki + memory)

Status: planned (not started). Decision: **any authenticated user** sees the
shared wiki graph + their **own** memory graph (per-user isolation enforced);
memory graph is rich (episodic→semantic + supersession leaves).

## Goal
A `/knowledge` page that renders a force-directed graph of the knowledge stores.
Selecting a node shows that node's content; a `[[linked page]]` in the content
selects + focuses that node and shows it.

## Grounding (from code survey)
- Frontend: React 19 + Vite + react-router + Tailwind/shadcn. Central
  `apiFetch<T>()` (`web/src/api/client.ts`) injects the Bearer token; 401 →
  `clearToken()`. `react-markdown` already a dep. **No graph lib yet.**
- Adding a page = route in `web/src/App.tsx` + add the path to the SPA-fallback
  loop AND `exempt_exact` in `agents/adk_cc/service/server.py` (_mount_ui,
  ~lines 185-201 / 109-115).
- Backend has **no wiki/memory content endpoints today** — only admin/config.
  Stores expose everything needed:
  - Wiki (shared, per-tenant): `WikiStore.for_tenant(tid)`; `list_domain_pages()`,
    `read_domain_page(slug)->Page`, `Page.wikilinks` (parses `[[slug|alias]]`),
    `Page.sources`, `Page.contested`, `list_inbox(user)`, `read_changelog()`.
  - Memory (per-user): `MemoryStore.for_tenant(tid)`; `list_semantic(user)`,
    `list_episodic(user)`, `get_topic_index(user)`, `MemoryItem.{topic,
    supersedes,sources,confidence,status}`.
- Tenant/user from `request.state.adk_cc_auth` (AuthPrincipal: user_id,
  tenant_id, roles). Admin mount pattern: `service/admin_routes.py`
  `mount_*` + `_authorize_for_tenant`.

## Backend — new `service/graph_routes.py` (gated, mounted in build_fastapi_app)
Authorize off `request.state.adk_cc_auth`; tenant from the principal (not a path
param, so a user can't request another tenant's graph). Memory endpoints scope
to the principal's OWN user_id — never a path user (reuses the isolation proven
in the security e2e).

- `GET /api/knowledge/wiki/graph` → `{nodes:[{id,label,kind:"domain"|"inbox",
  contested,sourceCount}], links:[{source,target}]}`. nodes = domain pages +
  caller's inbox overlay (distinct kind); links = `Page.wikilinks` (skip dangling
  targets or mark them `missing:true`).
- `GET /api/knowledge/wiki/page/{slug}` → `{title,body,frontmatter,contested,
  sources}`.
- `GET /api/knowledge/memory/graph` → nodes = semantic topics (kind:"semantic",
  +confidence,status) + episodic captures (kind:"episodic"); links =
  episodic→semantic (shared topic) + supersession leaves hanging off the semantic
  node. OWN user only.
- `GET /api/knowledge/memory/item/{id}` → full MemoryItem.

Gating flag: `ADK_CC_KNOWLEDGE_UI=1` (new; independent of the admin panel so a
non-admin deployment can still expose it). Page is for any authenticated user.

## Frontend
- Add `react-force-graph-2d` (light, 2D; canvas).
- `web/src/api/knowledge.ts`: typed `apiFetch` wrappers for the 4 endpoints.
- `web/src/pages/KnowledgePage.tsx`: **Wiki | Memory** tabs (mirror
  `AdminPage.tsx` tab pattern + `useAsync`). Split layout: graph canvas (left) +
  detail pane (right).
  - node click → fetch content → render in pane via `react-markdown`.
  - `[[slug]]` transform: a small remark plugin / regex pass turns wikilinks into
    clickable controls that (a) select+focus that node in the graph and (b) load
    its content. (react-markdown doesn't understand `[[ ]]` natively.)
  - node color by kind (domain / inbox / semantic / episodic); contested pages
    flagged; confidence → node size for memory.
- Wire `/knowledge` into `App.tsx`, the `server.py` SPA-fallback loop, and
  `exempt_exact`.

## Verification
- Model-free backend unit tests: seed WikiStore + MemoryStore for two users on
  one tenant → assert wiki graph nodes/links match pages+wikilinks; memory graph
  scoped to caller (bob never sees alice's nodes — the isolation assertion);
  dangling-link handling.
- Playwright smoke (webapp-testing skill): load `/knowledge`, both tabs render a
  graph, click a node → detail pane populates, click a `[[link]]` → focus
  changes + content loads.

## Open/■ deferred
- Edge richness for memory is inherently lower than wiki (memory is cluster-
  shaped); acceptable.
- Large graphs: add a node cap + "showing N of M" notice if a tenant's wiki is
  huge (don't silently truncate).
