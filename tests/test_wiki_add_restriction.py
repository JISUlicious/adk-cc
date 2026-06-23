"""wiki_add must not store personal USER info in the shared wiki (it belongs in
memory). Model-free."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
os.environ["ADK_CC_WIKI"] = "1"
os.environ["ADK_CC_WIKI_ROOT"] = tempfile.mkdtemp(prefix="waddr-")
for _k in ("ADK_CC_WIKI_STORE_URI",):
    os.environ.pop(_k, None)

from adk_cc.tools.schemas import WikiAddArgs
from adk_cc.tools.wiki import WikiAddTool, _personal_info_reason
from adk_cc.wiki import WikiStore


def _ctx(user="alice", tenant="acme"):
    return SimpleNamespace(
        state={"temp:tenant_context": SimpleNamespace(tenant_id=tenant, user_id=user)})


def _add(text, topic=None, title=None):
    return asyncio.run(WikiAddTool()._execute(
        WikiAddArgs(text=text, topic=topic, title=title), _ctx()))


# ---------- pure guard ----------
def test_personal_topic_blocked():
    for t in ("user-jisu", "user-profile-jisu", "user-name", "about-me", "my-bio", "profile"):
        assert _personal_info_reason("anything", t, None) is not None, t


def test_personal_text_blocked():
    for s in ("My name is Jisu.", "Remember about me: I like dark mode.",
              "I am a staff engineer.", "I prefer concise answers.",
              "The user's role is staff engineer."):
        assert _personal_info_reason(s, "notes", None) is not None, s


def test_domain_content_allowed():
    for s, t in [("Modern CPU cores use ~14-stage pipelines.", "pipeline-depth"),
                 ("L2 cache is 512KB per core.", "cache-hierarchy"),
                 ("TAGE is a branch predictor.", "branch-prediction"),
                 ("The deploy uses Postgres 16.", "datastore")]:
        assert _personal_info_reason(s, t, None) is None, (s, t)


# ---------- tool behavior ----------
def test_tool_skips_personal_and_writes_nothing():
    before = len(WikiStore.for_tenant("acme").list_inbox("alice"))
    r = _add("My name is Jisu and I'm a staff engineer.", topic="user-profile-jisu")
    assert r["status"] == "skipped" and r["reason"] == "personal_info", r
    after = WikiStore.for_tenant("acme").list_inbox("alice")
    assert len(after) == before, "skipped add must not write to the inbox"
    assert "user-profile-jisu" not in [d.slug for d in after]


def test_tool_saves_domain_doc():
    r = _add("Modern cores use TAGE branch predictors.", topic="branch-prediction")
    assert r["status"] == "ok" and r["slug"] == "branch-prediction", r
    slugs = [d.slug for d in WikiStore.for_tenant("acme").list_inbox("alice")]
    assert "branch-prediction" in slugs


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK {t.__name__[5:]}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__[5:]}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__[5:]}: {type(e).__name__}: {e}")
    print("\nall wiki-add-restriction tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
