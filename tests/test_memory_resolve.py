"""Fix A+D tests: identity resolution. Model-free paths (LLM path is probed live).

Covers: empty-store identity (old behavior), deterministic fallback merge onto an
existing token-matching topic, no-merge when distinct, the LLM-output parser, and
the verify gate's reject→downgrade-to-NEW.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.memory import MemoryStore, resolve_facts
from adk_cc.memory import resolve as R


def _store():
    return MemoryStore.for_tenant("acme", root=tempfile.mkdtemp(prefix="rslv-mem-"))


def test_empty_store_is_identity():
    # no existing topics + no model → every fact NEW under its proposed slug
    s = _store()
    out = asyncio.run(resolve_facts(None, s, "alice", [("Deploy Target", "fly")]))
    assert len(out) == 1 and out[0].action == "new" and out[0].topic == "deploy-target", out


def test_deterministic_fallback_merges_token_match():
    s = _store()
    s.add_episodic("alice", "deploys to fly", topic="deploy")  # existing topic {deploy}
    # model=None → deterministic path; proposed {deploy,target} ⊃ {deploy} → merge
    out = asyncio.run(resolve_facts(None, s, "alice", [("deploy target", "now on railway")]))
    assert out[0].topic == "deploy" and out[0].action == "update", out


def test_deterministic_fallback_no_merge_when_distinct():
    s = _store()
    s.add_episodic("alice", "uses postgres", topic="datastore")
    out = asyncio.run(resolve_facts(None, s, "alice", [("Editor", "prefers vim")]))
    assert out[0].action == "new" and out[0].topic == "editor", out


def test_parse_maps_actions_to_existing_topics():
    facts = [("profession", "embedded cores"), ("clock", "1ghz")]
    existing = {"user-profession": "works on embedded cores"}
    res = R._parse("1: CORROBORATE user-profession\n2: NEW", facts, existing)
    assert res[0].action == "corroborate" and res[0].topic == "user-profession"
    assert res[1].action == "new" and res[1].topic == "clock"


def test_parse_downgrades_unknown_topic_to_new():
    facts = [("x", "fact")]
    res = R._parse("1: UPDATE nonexistent-topic", facts, {"real-topic": "..."})
    assert res[0].action == "new", res  # named a topic that doesn't exist → safe NEW


def test_verify_reject_downgrades_to_new():
    from types import SimpleNamespace

    # model that answers "NO" to the verify question (proper async generator)
    class _NoModel:
        async def generate_content_async(self, req, stream=False):
            yield SimpleNamespace(content=SimpleNamespace(
                parts=[SimpleNamespace(text="NO", thought=False)]))

    r = R.Resolution(fact="f", topic="existing", action="corroborate", proposed="prop")
    out = asyncio.run(R._verify(_NoModel(), r, {"existing": "summary"}))
    assert out.action == "new" and out.topic == "prop", out
    assert out.verified is False and out.reason == "verify_rejected", out


def test_verify_yes_keeps_merge():
    from types import SimpleNamespace

    class _YesModel:
        async def generate_content_async(self, req, stream=False):
            yield SimpleNamespace(content=SimpleNamespace(
                parts=[SimpleNamespace(text="YES", thought=False)]))

    r = R.Resolution(fact="f", topic="existing", action="update", proposed="prop")
    out = asyncio.run(R._verify(_YesModel(), r, {"existing": "summary"}))
    assert out.action == "update" and out.topic == "existing", out


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
    print("\nall resolve tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
