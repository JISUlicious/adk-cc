"""In-band wiki tools: search / read / add (ADK_CC_WIKI=1).

These are the interactive surface of the memory system:

  - `wiki_search` / `wiki_read` — query the shared domain wiki overlaid
    with the caller's own private notes (read-only).
  - `wiki_add` — capture a doc/claim into the caller's PRIVATE inbox. It
    never edits the shared wiki; the offline librarian merges vetted notes
    into domain later. This single-writer rule is what keeps concurrent
    captures from corrupting the shared wiki with un-resolvable conflicts.

Scope comes from the tenant context TenancyPlugin seeds into session state
(`temp:tenant_context`), falling back to `local`/`local` in single-user dev
— so these tools work unchanged on the flat dev path.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from ..wiki import WikiStore
from ..wiki import page as pagelib
from ..wiki import search as searchlib
from .base import AdkCcTool, ToolMeta
from .schemas import WikiAddArgs, WikiReadArgs, WikiSearchArgs

_TENANT_KEY = "temp:tenant_context"


def _store_and_user(ctx: ToolContext) -> tuple[WikiStore, str]:
    """Resolve (WikiStore, user_id) from the session's tenant context.
    Degrades to local/local in dev so the flat path Just Works."""
    state = getattr(ctx, "state", None)
    tc = state.get(_TENANT_KEY) if hasattr(state, "get") else None
    tenant_id = getattr(tc, "tenant_id", None) or "local"
    user_id = getattr(tc, "user_id", None) or "local"
    store = WikiStore.for_tenant(tenant_id).ensure()
    return store, user_id


class WikiSearchTool(AdkCcTool):
    meta = ToolMeta(
        name="wiki_search",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = WikiSearchArgs
    description = (
        "Search the knowledge wiki (shared domain wiki + your private notes) "
        "for pages relevant to a query. Returns ranked hits tagged by scope."
    )

    async def _execute(self, args: WikiSearchArgs, ctx: ToolContext) -> dict[str, Any]:
        store, user_id = _store_and_user(ctx)
        hits = searchlib.search(
            store, args.query, user_id=user_id, limit=args.limit
        )
        return {
            "status": "ok",
            "query": args.query,
            "count": len(hits),
            "hits": [
                {
                    "slug": h.slug,
                    "title": h.title,
                    "scope": h.scope,
                    "contested": h.contested,
                    "snippet": h.snippet,
                }
                for h in hits
            ],
        }


class WikiReadTool(AdkCcTool):
    meta = ToolMeta(
        name="wiki_read",
        is_read_only=True,
        is_concurrency_safe=True,
    )
    input_model = WikiReadArgs
    description = (
        "Read a wiki page by slug. scope=auto prefers your private note on "
        "the topic and falls back to the shared wiki; domain/inbox force one."
    )

    async def _execute(self, args: WikiReadArgs, ctx: ToolContext) -> dict[str, Any]:
        store, user_id = _store_and_user(ctx)
        slug = pagelib.slugify(args.slug)
        scope = (args.scope or "auto").lower()

        inbox_page = None
        if scope in ("auto", "inbox"):
            for doc in store.list_inbox(user_id):
                if doc.slug == slug:
                    inbox_page = doc.page
                    break
        domain_page = None
        if scope in ("auto", "domain"):
            domain_page = store.read_domain_page(slug)

        chosen, chosen_scope = None, None
        if scope == "inbox":
            chosen, chosen_scope = inbox_page, "inbox"
        elif scope == "domain":
            chosen, chosen_scope = domain_page, "domain"
        else:  # auto: private note shadows shared wiki for this user
            if inbox_page is not None:
                chosen, chosen_scope = inbox_page, "inbox"
            else:
                chosen, chosen_scope = domain_page, "domain"

        if chosen is None:
            return {
                "status": "not_found",
                "slug": slug,
                "error": f"no {scope} page for slug {slug!r}",
            }
        # When auto-reading the private note, tell the model the shared wiki
        # also has this topic so it can reconcile rather than assume.
        also_in_domain = (
            chosen_scope == "inbox" and store.read_domain_page(slug) is not None
        )
        return {
            "status": "ok",
            "slug": slug,
            "scope": chosen_scope,
            "title": chosen.title,
            "contested": chosen.contested,
            "frontmatter": chosen.frontmatter,
            "content": chosen.body,
            "also_in_shared_wiki": also_in_domain,
        }


class WikiAddTool(AdkCcTool):
    # Writes ONLY to the caller's private inbox (host-side wiki store, not
    # the sandbox) — low-risk, so not destructive and no confirmation gate.
    meta = ToolMeta(
        name="wiki_add",
        is_read_only=False,
        is_concurrency_safe=False,
        is_destructive=False,
        needs_sandbox=False,
    )
    input_model = WikiAddArgs
    description = (
        "Capture a note/document into YOUR private wiki inbox. It is NOT "
        "added to the shared wiki directly — the librarian merges vetted "
        "notes into the shared domain wiki periodically. Set `topic` to an "
        "existing page's subject to have your note merged into it."
    )

    async def _execute(self, args: WikiAddArgs, ctx: ToolContext) -> dict[str, Any]:
        store, user_id = _store_and_user(ctx)
        doc = store.add_inbox(
            user_id,
            args.text,
            title=args.title,
            topic=args.topic,
        )
        return {
            "status": "ok",
            "scope": "inbox",
            "doc_id": doc.doc_id,
            "slug": doc.slug,
            "title": doc.page.title,
            "note": (
                "Saved to your private notes. It will be reviewed and merged "
                "into the shared wiki by the librarian."
            ),
        }
