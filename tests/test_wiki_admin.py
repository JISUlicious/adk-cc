"""#129-2: admin wiki document management + the human-edit survival rule.

THE load-bearing A/B: a page an owner hand-edited must survive a librarian
run that carries a conflicting (superseding) inbox note — the claim is held
for adjudication instead. Accepting the claim later lets the supersession
through; the same setup WITHOUT the human-edit stamp auto-supersedes.

Also: store.adjudicate (sticky + queue close), and the HTTP routes
(page CRUD, review queue, adjudication) through mount_tenant_admin.

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_wiki_admin.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_WIKI"] = "1"

from starlette.requests import Request  # noqa: E402,F401 — get_type_hints

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


OLD = "All deploys go through the staging cluster first."
NEW = "Deploys now go directly to prod; staging was retired."


def _store(tmp):
    os.environ["ADK_CC_WIKI_ROOT"] = tmp
    from adk_cc.wiki import WikiStore

    return WikiStore.for_tenant("local").ensure()


def _seed(store, *, human_edited: bool):
    from adk_cc.wiki.page import Page

    fm = {"title": "Deploy process"}
    if human_edited:
        fm["human_edited"] = "2026-08-19T00:00:00+00:00"
        fm["edited_by"] = "alice"
    store.write_domain_page(Page(slug="deploy-process", frontmatter=fm,
                                 body=OLD + "\n"))
    store.add_inbox("u1", NEW, topic="deploy-process")


def _run_librarian(store):
    from adk_cc.wiki import conflict
    from adk_cc.wiki.conflict import Verdict
    from adk_cc.wiki.librarian import Librarian

    def classify(claim, page):
        return Verdict(conflict.SUPERSESSION, reason="newer info")

    return asyncio.run(Librarian(store, classifier=classify).run())


def main() -> int:
    # ---- adjudicate unit -------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.add_quarantine("abc123", {"claim_text": "x"})
        rec = st.adjudicate("abc123", action="accept", note="looks right")
        check("adjudicate closes the queue record",
              rec["status"] == "accepted"
              and not st.list_quarantine(pending_only=True))
        check("adjudicate writes the STICKY human resolution",
              st.human_override("abc123") == "accept")
        try:
            st.adjudicate("abc123", action="maybe")
            check("adjudicate rejects bad actions", False)
        except ValueError:
            check("adjudicate rejects bad actions", True)

    # ---- THE A/B: human edit survives a conflicting librarian run --------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        _seed(st, human_edited=True)
        report = _run_librarian(st)
        page = st.read_domain_page("deploy-process")
        check("A: human-edited body SURVIVES the superseding note",
              OLD in page.body and NEW not in page.body, page.body[:120])
        check("A: protection counted in the report",
              report.actions.get("human_edit_protected", 0) == 1,
              report.actions)
        queue = st.list_quarantine(pending_only=True)
        check("A: the claim is queued for adjudication", len(queue) == 1)

        # accept → the NEXT run applies the supersession
        st.adjudicate(queue[0]["claim_hash"], action="accept", note="true")
        _run_librarian(st)
        page2 = st.read_domain_page("deploy-process")
        check("A: accepted claim supersedes on the next run",
              NEW in page2.body, page2.body[:120])
        check("A: human-edit stamp persists through the accepted supersede",
              page2.frontmatter.get("human_edited"))

    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        _seed(st, human_edited=False)
        _run_librarian(st)
        page = st.read_domain_page("deploy-process")
        check("B (control): without the stamp the note auto-supersedes",
              NEW in page.body, page.body[:120])

    # ---- reject path: page stays clean, claim closed ---------------------
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        _seed(st, human_edited=True)
        _run_librarian(st)
        queue = st.list_quarantine(pending_only=True)
        st.adjudicate(queue[0]["claim_hash"], action="reject", note="wrong")
        _run_librarian(st)
        page = st.read_domain_page("deploy-process")
        check("reject: page keeps the human text on later runs",
              OLD in page.body and NEW not in page.body)

    # ---- HTTP routes -----------------------------------------------------
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from adk_cc.credentials import InMemoryCredentialProvider
    from adk_cc.service.admin_routes import mount_tenant_admin
    from adk_cc.service.auth import (
        AuthPrincipal, BearerTokenExtractor, make_auth_middleware,
    )
    from adk_cc.service.registry import JsonFileTenantResourceRegistry
    from adk_cc.tools.mcp_tenant import McpServerConfig

    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)  # sets ADK_CC_WIKI_ROOT before mount
        tokmap = {"admintok": AuthPrincipal("alice", "local",
                                            frozenset({"admin"}))}
        app = FastAPI()
        mount_tenant_admin(
            app,
            registry=JsonFileTenantResourceRegistry[McpServerConfig](
                root=os.path.join(tmp, "registry"), kind="mcp",
                model=McpServerConfig, id_attr="server_name"),
            credentials=InMemoryCredentialProvider(shared=False),
        )
        app.add_middleware(make_auth_middleware(BearerTokenExtractor(tokmap)))
        c = TestClient(app)
        h = {"Authorization": "Bearer admintok"}

        r = c.put("/tenants/local/wiki-pages/deploy-process",
                  json={"body": OLD, "frontmatter": {"title": "Deploys"}},
                  headers=h)
        check("route: PUT creates/edits and stamps human_edited",
              r.status_code == 200 and r.json().get("human_edited"),
              (r.status_code, r.text[:120]))
        r = c.get("/tenants/local/wiki-pages/deploy-process", headers=h)
        check("route: GET returns body + stamped frontmatter",
              r.status_code == 200 and OLD in r.json()["body"]
              and r.json()["frontmatter"].get("edited_by") == "alice")
        r = c.get("/tenants/local/wiki-pages", headers=h)
        check("route: list shows the page with the stamp",
              any(p["slug"] == "deploy-process" and p["human_edited"]
                  for p in r.json()["pages"]))

        # conflict → review queue over HTTP
        st.add_inbox("u1", NEW, topic="deploy-process")
        _run_librarian(st)
        r = c.get("/tenants/local/wiki-review", headers=h)
        check("route: review queue lists the held claim",
              r.status_code == 200 and len(r.json()["queue"]) == 1,
              r.text[:150])
        ch = r.json()["queue"][0]["claim_hash"]
        r = c.post(f"/tenants/local/wiki-review/{ch}",
                   json={"action": "accept", "note": "confirmed"}, headers=h)
        check("route: adjudication accepted",
              r.status_code == 200 and r.json()["record"]["status"] == "accepted")
        _run_librarian(st)
        check("route: accepted claim lands in the page",
              NEW in st.read_domain_page("deploy-process").body)

        r = c.delete("/tenants/local/wiki-pages/deploy-process", headers=h)
        check("route: DELETE removes the page", r.status_code == 200)
        r = c.get("/tenants/local/wiki-pages/deploy-process", headers=h)
        check("route: GET after delete is 404", r.status_code == 404)
        r = c.put("/tenants/local/wiki-pages/x", json={}, headers=h)
        check("route: PUT without body is 400", r.status_code == 400)
        r = c.get("/tenants/local/wiki-pages", headers=None)
        check("route: unauthenticated is rejected",
              r.status_code in (401, 403), r.status_code)

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
