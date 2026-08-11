"""Read-only graph endpoints for the knowledge visualizer (analysis/
knowledge-graph-plan.md). Gated by ADK_CC_KNOWLEDGE_UI=1.

Serves a force-graph of the shared wiki (domain pages + the caller's inbox
overlay, edges from [[wikilinks]]) and the caller's OWN memory (semantic topics
+ episodic captures, edges by shared topic). Memory is strictly scoped to the
authenticated user — never a path/query param — preserving the per-user
isolation proven in the security e2e. Any authenticated user may view.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from ..config.schema import env_bool


def knowledge_ui_enabled() -> bool:
    return env_bool("ADK_CC_KNOWLEDGE_UI")


def _principal(request) -> tuple[str, str]:
    """(tenant_id, user_id) for scoping the graph.

    Authenticated (web): from the principal — NEVER a query param, preserving the
    per-user isolation proven in the security e2e.

    No-auth (desktop OR dev web): there is no principal and no cross-user
    boundary to breach — `?user=` selects the scope, exactly as the shells
    pass it (desktop: the project id; no-auth web: the session's user id).
    A hardcoded 'local' here made capture (session uid) and display
    (principal) read DIFFERENT stores on no-auth web — wiki_add notes
    existed but never rendered. Falls back to 'local'."""
    auth = getattr(request.state, "adk_cc_auth", None)
    if auth is None:
        return "local", (request.query_params.get("user") or "local")
    try:
        user_id, tenant_id = auth[0], auth[1]
        return (tenant_id or "local"), (user_id or "local")
    except Exception:
        return "local", "local"


def mount_knowledge_routes(app) -> None:
    """Attach the /api/knowledge/* routes when enabled. No-op otherwise."""
    if not knowledge_ui_enabled():
        return

    from ..memory import MemoryStore
    from ..wiki import WikiStore, slugify

    @app.get("/api/knowledge/wiki/graph", include_in_schema=False)
    def _wiki_graph(request: Request):  # noqa: ANN202
        tenant_id, user_id = _principal(request)
        wiki = WikiStore.for_tenant(tenant_id).ensure()
        slugs = list(wiki.list_domain_pages())
        known = set(slugs)
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        for slug in slugs:
            page = wiki.read_domain_page(slug)
            if page is None:
                continue
            nodes.append({
                "id": slug,
                "label": page.title,
                "kind": "domain",
                "type": page.type,
                "tags": page.tags,
                "contested": bool(page.contested),
                "sources": len(page.sources),
            })
            for target in page.wikilinks:
                links.append({
                    "source": slug,
                    "target": target,
                    "missing": target not in known,
                })
        # caller's inbox overlay (distinct kind). One node PER SLUG — a user can
        # have several notes under the same topic; the overlay means "you have a
        # private note on this topic", so collapse them (avoids duplicate node
        # ids, which break the force-graph). `notes` records how many.
        inbox_seen: dict[str, dict] = {}
        for doc in wiki.list_inbox(user_id):
            n = inbox_seen.get(doc.slug)
            if n is None:
                n = {"id": f"inbox:{doc.slug}", "label": doc.slug,
                     "kind": "inbox", "notes": 0}
                inbox_seen[doc.slug] = n
                nodes.append(n)
                if doc.slug in known:
                    links.append({"source": n["id"], "target": doc.slug, "overlay": True})
            n["notes"] += 1
        # #129-3: the caller's PERSONAL wiki pages (librarian-consolidated
        # from their notes). Distinct kind + id namespace — a personal page
        # and a domain page may share a slug.
        from ..wiki import PersonalWikiView

        personal = PersonalWikiView(wiki, user_id)
        pslugs = set(personal.list_domain_pages())
        for slug in sorted(pslugs):
            page = personal.read_domain_page(slug)
            if page is None:
                continue
            nodes.append({
                "id": f"personal:{slug}",
                "label": page.title,
                "kind": "personal",
                "type": page.type,
                "tags": page.tags,
                "contested": bool(page.contested),
                "sources": len(page.sources),
            })
            for target in page.wikilinks:
                links.append({
                    "source": f"personal:{slug}",
                    "target": (f"personal:{target}" if target in pslugs
                               else target),
                    "missing": target not in pslugs and target not in known,
                })
        return {"nodes": nodes, "links": links}

    @app.get("/api/knowledge/wiki/personal/{slug}", include_in_schema=False)
    def _wiki_personal_page(slug: str, request: Request):  # noqa: ANN202
        """The CALLER'S personal wiki page — principal-scoped like every
        route here; there is no way to read another user's personal wiki."""
        from ..wiki import PersonalWikiView

        tenant_id, user_id = _principal(request)
        wiki = WikiStore.for_tenant(tenant_id).ensure()
        page = PersonalWikiView(wiki, user_id).read_domain_page(slugify(slug))
        if page is None:
            return {"status": "not_found", "slug": slug}
        return {
            "status": "ok",
            "slug": page.slug,
            "title": page.title,
            "contested": bool(page.contested),
            "frontmatter": page.frontmatter,
            "body": page.body,
            "sources": page.sources,
        }

    @app.get("/api/knowledge/wiki/inbox/{slug}", include_in_schema=False)
    def _wiki_inbox(slug: str, request: Request):  # noqa: ANN202
        """The CALLER'S unmerged inbox notes for one slug — content, not a
        stub. Principal-scoped like every route here; there is no way to
        read another user's inbox."""
        tenant_id, user_id = _principal(request)
        wiki = WikiStore.for_tenant(tenant_id).ensure()
        want = slugify(slug)
        notes = [{"doc_id": d.doc_id, "text": d.page.body,
                  "frontmatter": d.page.frontmatter}
                 for d in wiki.list_inbox(user_id) if d.slug == want]
        if not notes:
            return {"status": "not_found", "slug": slug}
        return {"status": "ok", "slug": want, "notes": notes}

    @app.get("/api/knowledge/wiki/page/{slug}", include_in_schema=False)
    def _wiki_page(slug: str, request: Request):  # noqa: ANN202
        tenant_id, _ = _principal(request)
        wiki = WikiStore.for_tenant(tenant_id).ensure()
        page = wiki.read_domain_page(slugify(slug))
        if page is None:
            return {"status": "not_found", "slug": slug}
        return {
            "status": "ok",
            "slug": page.slug,
            "title": page.title,
            "contested": bool(page.contested),
            "frontmatter": page.frontmatter,
            "body": page.body,
            "sources": page.sources,
        }

    @app.get("/api/knowledge/memory/graph", include_in_schema=False)
    def _memory_graph(request: Request):  # noqa: ANN202
        tenant_id, user_id = _principal(request)  # OWN user only
        mem = MemoryStore.for_tenant(tenant_id)
        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        sem_topics = set()
        for item in mem.list_semantic(user_id):
            sem_topics.add(item.topic)
            nodes.append({
                "id": f"sem:{item.id}",
                "label": item.topic,
                "kind": "semantic",
                "confidence": item.confidence,
                "status": item.status,
                "topic": item.topic,
            })
        for item in mem.list_episodic(user_id):
            nid = f"epi:{item.id}"
            nodes.append({
                "id": nid,
                "label": item.topic,
                "kind": "episodic",
                "status": item.status,
                "topic": item.topic,
            })
            # episodic → its semantic topic (if consolidated into one)
            for s in mem.list_semantic(user_id):
                if s.topic == item.topic:
                    links.append({"source": nid, "target": f"sem:{s.id}"})
                    break
        return {"nodes": nodes, "links": links}

    @app.get("/api/knowledge/memory/item/{item_id}", include_in_schema=False)
    def _memory_item(item_id: str, request: Request):  # noqa: ANN202
        tenant_id, user_id = _principal(request)
        mem = MemoryStore.for_tenant(tenant_id)
        for tier_items in (mem.list_semantic(user_id), mem.list_episodic(user_id)):
            for item in tier_items:
                if item.id == item_id:
                    return {
                        "status": "ok",
                        "id": item.id,
                        "topic": item.topic,
                        "text": item.text,
                        "memory_type": item.memory_type,
                        "item_status": item.status,
                        "confidence": item.confidence,
                        "sources": item.sources,
                        "supersedes": item.supersedes,
                        "created": item.created,
                        "updated": item.updated,
                    }
        return {"status": "not_found", "id": item_id}
