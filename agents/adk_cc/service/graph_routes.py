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


def knowledge_ui_enabled() -> bool:
    return os.environ.get("ADK_CC_KNOWLEDGE_UI") == "1"


def _principal(request) -> tuple[str, str]:
    """(tenant_id, user_id) from the authenticated principal; ('local','local')
    in no-auth dev. AuthPrincipal is the (user_id, tenant_id) tuple."""
    auth = getattr(request.state, "adk_cc_auth", None)
    if auth is None:
        return "local", "local"
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
                "contested": bool(page.contested),
                "sources": len(page.sources),
            })
            for target in page.wikilinks:
                links.append({
                    "source": slug,
                    "target": target,
                    "missing": target not in known,
                })
        # caller's inbox overlay (distinct kind), if any
        for doc in wiki.list_inbox(user_id):
            nid = f"inbox:{doc.slug}"
            nodes.append({"id": nid, "label": doc.slug, "kind": "inbox"})
            if doc.slug in known:
                links.append({"source": nid, "target": doc.slug, "overlay": True})
        return {"nodes": nodes, "links": links}

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
