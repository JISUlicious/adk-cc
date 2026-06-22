"""Phase 3 tests: memory→compaction seeding + recall-survives-compaction.
Model-free."""

from __future__ import annotations

import contextvars
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.agent import _seed_memory_into_summary
from adk_cc.memory import MemoryStore, set_principal


def _event(text="1. Primary Request: build a service."):
    part = SimpleNamespace(text=text)
    content = SimpleNamespace(parts=[part], role="model")
    return SimpleNamespace(actions=SimpleNamespace(
        compaction=SimpleNamespace(compacted_content=content)))


def _user_events(text):
    c = types.Content(role="user", parts=[types.Part(text=text)])
    return [SimpleNamespace(content=c)]


def _text(ev):
    return ev.actions.compaction.compacted_content.parts[0].text


def _seed_store(root, tenant="acme", user="alice"):
    s = MemoryStore.for_tenant(tenant, root=root)
    # two consolidations make a semantic item recall can find
    s.add_episodic(user, "The user's project deploys to Fly.io.", topic="deploy-target")
    s.add_episodic(user, "The user's project deploys to Fly.io.", topic="deploy-target")
    from adk_cc.memory import consolidate_user
    consolidate_user(s, user)
    return s


def _with_env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    return saved


def _restore(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_seed_disabled_by_default():
    root = tempfile.mkdtemp(prefix="seed-")
    saved = _with_env(ADK_CC_MEMORY_ROOT=root)
    os.environ.pop("ADK_CC_COMPACTION_SEED_MEMORY", None)
    try:
        _seed_store(root)
        set_principal("acme", "alice")
        ev = _event()
        before = _text(ev)
        _seed_memory_into_summary(ev, _user_events("where does my project deploy?"))
        assert _text(ev) == before, "must be inert when SEED_MEMORY unset"
    finally:
        _restore(saved)


def test_seed_prepends_recalled_facts():
    root = tempfile.mkdtemp(prefix="seed-")
    saved = _with_env(ADK_CC_MEMORY_ROOT=root, ADK_CC_COMPACTION_SEED_MEMORY="1")
    try:
        _seed_store(root)
        set_principal("acme", "alice")
        ev = _event()
        _seed_memory_into_summary(ev, _user_events("where does the project deploy?"))
        out = _text(ev)
        assert out.startswith("Durable facts about this user"), out[:60]
        assert "Fly.io" in out
        assert "Primary Request" in out  # original summary still present
    finally:
        _restore(saved)


def test_seed_noop_without_principal():
    root = tempfile.mkdtemp(prefix="seed-")
    saved = _with_env(ADK_CC_MEMORY_ROOT=root, ADK_CC_COMPACTION_SEED_MEMORY="1")
    try:
        _seed_store(root)
        ev = _event()
        before = _text(ev)
        # run in a FRESH context so the principal contextvar is unset (None)
        ctx = contextvars.Context()
        ctx.run(_seed_memory_into_summary, ev, _user_events("deploy?"))
        assert _text(ev) == before, "no principal → no seed"
    finally:
        _restore(saved)


def test_seed_noop_when_recall_empty():
    root = tempfile.mkdtemp(prefix="seed-")
    saved = _with_env(ADK_CC_MEMORY_ROOT=root, ADK_CC_COMPACTION_SEED_MEMORY="1")
    try:
        _seed_store(root)  # alice has memory; bob has none
        set_principal("acme", "bob")
        ev = _event()
        before = _text(ev)
        _seed_memory_into_summary(ev, _user_events("anything?"))
        assert _text(ev) == before, "empty recall → no seed"
    finally:
        _restore(saved)


def test_recall_survives_compaction_safety_net():
    """MemoryPlugin recall injects durable facts into the request regardless of
    compaction — so even when the summary drops them, they're re-present every
    turn. Here we prove recall produces the block for a short (post-compaction-
    like) request."""
    root = tempfile.mkdtemp(prefix="seed-")
    saved = _with_env(ADK_CC_MEMORY_ROOT=root)
    try:
        s = _seed_store(root)
        from adk_cc.memory import recall_context
        block = recall_context(s, "alice", "where does my project deploy?")
        assert "Fly.io" in block and "Known facts" in block
    finally:
        _restore(saved)


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
    print("\nall compaction-seed tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
