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

import re
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from ..wiki import WikiStore
from ..wiki import page as pagelib
from ..wiki import search as searchlib
from .base import AdkCcTool, ToolMeta
from .schemas import WikiAddArgs, WikiReadArgs, WikiSearchArgs

_TENANT_KEY = "temp:tenant_context"

# The shared wiki is for durable DOMAIN knowledge, not personal user facts
# (name/role/preferences/identity) — those belong in the per-user memory system.
# Conservative, high-precision signals so genuine domain docs aren't blocked.
# A topic slug naming a person/profile:
_PERSONAL_TOPIC_RE = re.compile(
    r"^(user(-|$)|about-me|my-|profile$|bio$|user-profile)", re.IGNORECASE)
# First-person identity / preference / memory-directive phrasing:
_PERSONAL_TEXT_RE = re.compile(
    r"\b(my name is|remember (about )?me|i am (a|an|the)\b.*\b(engineer|developer|"
    r"manager|designer|lead|architect|scientist)|i (prefer|like|use|work)\b|"
    r"the user'?s? (name|role|identity|preference|profile))", re.IGNORECASE)


def _personal_info_reason(text: str, topic: Optional[str], title: Optional[str]) -> Optional[str]:
    """Return a short reason string when a wiki_add looks like personal user
    info (so it should go to memory, not the shared wiki), else None."""
    slug = pagelib.slugify(topic or title or "")
    if slug and _PERSONAL_TOPIC_RE.search(slug):
        return f"topic:{slug}"
    m = _PERSONAL_TEXT_RE.search(text or "")
    if m:
        return f"text:{m.group(0)[:40]}"
    return None


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
        "Capture a SHARED-KNOWLEDGE note/document into YOUR private wiki inbox; "
        "the librarian later merges vetted notes into the team's shared domain "
        "wiki. Use it ONLY for durable, reusable domain/project knowledge that "
        "would help anyone (facts, designs, decisions, how-tos). Do NOT use it "
        "for personal facts about the user (their name, role, preferences, "
        "identity) — those are remembered automatically by the memory system and "
        "must not go into the shared wiki. Set `topic` to an existing page's "
        "subject to merge into it."
    )

    async def _execute(self, args: WikiAddArgs, ctx: ToolContext) -> dict[str, Any]:
        # Enforce the shared-vs-personal boundary: the shared wiki is not the
        # place for user identity/preferences (memory handles those). Decline
        # such captures rather than polluting the domain wiki.
        reason = _personal_info_reason(args.text or "", args.topic, args.title)
        if reason is not None:
            return {
                "status": "skipped",
                "reason": "personal_info",
                "note": (
                    "Not added to the shared wiki: this looks like personal "
                    "info about the user, which the memory system remembers "
                    "automatically. The shared wiki is for durable domain "
                    "knowledge that helps everyone."
                ),
                "matched": reason,
            }
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
