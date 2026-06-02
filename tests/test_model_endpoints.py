"""Tests for model-endpoint management (Phase 2).

Covers:
  - ModelEndpointRegistry CRUD + active pointer + persistence + guards;
  - SelectableLlm resolving the active endpoint per call (live switch) with a
    fake delegate (no real model connection);
  - the model-endpoint admin routes behind the admin gate (list masks
    secrets, put/delete/activate, last/active guards → 409/404).

Hand-rolled (no pytest).
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from starlette.requests import Request  # noqa: E402,F401 — get_type_hints

from adk_cc.models import ModelEndpointConfig, ModelEndpointRegistry, SelectableLlm


def _reg(tmp):
    return ModelEndpointRegistry(os.path.join(tmp, "models.json"))


def _cfg(name, model="openai/m", base="http://x/v1"):
    return ModelEndpointConfig(name=name, model=model, api_base=base)


# --- registry -------------------------------------------------------------

def test_first_endpoint_becomes_active():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(_cfg("a"))
        assert r.active_name() == "a"
        r.upsert(_cfg("b"))
        assert r.active_name() == "a"  # adding more doesn't change active
    print("OK test_first_endpoint_becomes_active")


def test_activate_and_persist():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "models.json")
        r = ModelEndpointRegistry(path)
        r.upsert(_cfg("a")); r.upsert(_cfg("b"))
        r.activate("b")
        # fresh instance reads persisted state
        r2 = ModelEndpointRegistry(path)
        assert r2.active_name() == "b"
        assert sorted(e.name for e in r2.list()) == ["a", "b"]
    print("OK test_activate_and_persist")


def test_activate_unknown_raises():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp); r.upsert(_cfg("a"))
        try:
            r.activate("nope")
            assert False, "expected ValueError"
        except ValueError:
            pass
    print("OK test_activate_unknown_raises")


def test_remove_guards():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp); r.upsert(_cfg("a")); r.upsert(_cfg("b"))  # active=a
        try:
            r.remove("a")  # active
            assert False
        except ValueError as e:
            assert "active" in str(e)
        r.activate("b")
        r.remove("a")  # now removable
        try:
            r.remove("b")  # last
            assert False
        except ValueError as e:
            assert "last" in str(e)
    print("OK test_remove_guards")


def test_seed_default_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.seed_default(_cfg("boot"))
        r.seed_default(_cfg("other"))  # no-op once populated
        assert [e.name for e in r.list()] == ["boot"]
        assert r.active_name() == "boot"
    print("OK test_seed_default_idempotent")


def test_masked_never_leaks_key():
    os.environ["MY_KEY"] = "supersecret"
    try:
        m = ModelEndpointConfig(name="x", model="m", api_base="u", api_key_env="MY_KEY").masked()
        assert m["api_key_present"] is True
        assert "supersecret" not in str(m)
        assert "api_key" not in m or m.get("api_key") is None  # no raw key field
    finally:
        os.environ.pop("MY_KEY", None)
    print("OK test_masked_never_leaks_key")


# --- SelectableLlm (live switch, fake delegate) ---------------------------

class _FakeLlm:
    """Stand-in BaseLlm-like delegate; records which model it represents."""
    def __init__(self, model):
        self.model = model

    async def generate_content_async(self, llm_request, stream=False):
        yield f"resp-from-{self.model}"


def test_selectable_resolves_active_per_call(monkeypatch_build=None):
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(_cfg("a", model="openai/aaa"))
        r.upsert(_cfg("b", model="anthropic/bbb"))
        sel = SelectableLlm(registry=r, default_model_id="boot")
        # Patch the LiteLlm builder so no real client is constructed.
        sel._build_litellm = lambda cfg: _FakeLlm(cfg.model)  # type: ignore

        d1 = sel._resolve_delegate()
        assert d1.model == "openai/aaa" and sel.model == "openai/aaa"
        # switch active → next resolve picks the new endpoint
        r.activate("b")
        d2 = sel._resolve_delegate()
        assert d2.model == "anthropic/bbb" and sel.model == "anthropic/bbb"
        # cached: re-resolving 'a' returns the same delegate object
        r.activate("a")
        assert sel._resolve_delegate() is d1
    print("OK test_selectable_resolves_active_per_call")


def test_selectable_falls_back_to_default_when_no_active():
    # No registry → uses the default delegate.
    sel = SelectableLlm(registry=None, default_delegate=_FakeLlm("boot"), default_model_id="boot")
    d = sel._resolve_delegate()
    assert d.model == "boot"
    print("OK test_selectable_falls_back_to_default_when_no_active")


def test_selectable_lazy_registry_from_env():
    # Regression guard: the agent builds SelectableLlm at import (before the
    # admin panel sets the registry-file env var), so the registry MUST be
    # resolved lazily from the env var — not captured at construction.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "models.json")
        # Construct BEFORE the env var exists (mirrors import-then-make_app).
        sel = SelectableLlm(
            registry_path_env="ADK_CC_TEST_MODEL_REG",
            default_delegate=_FakeLlm("boot"),
            default_model_id="boot",
        )
        sel._build_litellm = lambda cfg: _FakeLlm(cfg.model)  # type: ignore
        # No env yet → falls through to boot.
        assert sel._resolve_delegate().model == "boot"
        # Now configure + seed (as _prepare_admin_env would).
        r = ModelEndpointRegistry(path)
        r.upsert(_cfg("live", model="openai/live"))
        os.environ["ADK_CC_TEST_MODEL_REG"] = path
        try:
            # Lazy resolution now picks up the registry + active endpoint.
            assert sel._resolve_delegate().model == "openai/live"
        finally:
            os.environ.pop("ADK_CC_TEST_MODEL_REG", None)
    print("OK test_selectable_lazy_registry_from_env")


def test_selectable_generate_delegates():
    async def main():
        with tempfile.TemporaryDirectory() as tmp:
            r = _reg(tmp); r.upsert(_cfg("a", model="openai/aaa"))
            sel = SelectableLlm(registry=r)
            sel._build_litellm = lambda cfg: _FakeLlm(cfg.model)  # type: ignore
            out = [x async for x in sel.generate_content_async(object())]
            assert out == ["resp-from-openai/aaa"], out
    asyncio.run(main())
    print("OK test_selectable_generate_delegates")


# --- admin routes ---------------------------------------------------------

def _client(tmp):
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from adk_cc.service.auth import AuthPrincipal, BearerTokenExtractor, make_auth_middleware
    from adk_cc.service.admin_routes import mount_model_admin

    reg = _reg(tmp)
    reg.seed_default(_cfg("default", model="openai/boot"))

    def authorize(request, target):
        from fastapi import HTTPException
        auth = getattr(request.state, "adk_cc_auth", None)
        if auth is None:
            raise HTTPException(401, "no auth")
        if "admin" not in (getattr(auth, "roles", frozenset()) or frozenset()):
            raise HTTPException(403, "need admin")

    app = FastAPI()
    mount_model_admin(app, registry=reg, authorize=authorize)
    tokmap = {
        "admintok": AuthPrincipal("alice", "local", frozenset({"admin"})),
        "usertok": AuthPrincipal("bob", "local", frozenset()),
    }
    app.add_middleware(make_auth_middleware(BearerTokenExtractor(tokmap)))
    return TestClient(app), reg


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def test_routes_admin_gate():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        assert c.get("/admin/model-endpoints", headers=_h("usertok")).status_code == 403
        assert c.get("/admin/model-endpoints").status_code == 401
        assert c.get("/admin/model-endpoints", headers=_h("admintok")).status_code == 200
    print("OK test_routes_admin_gate")


def test_routes_list_put_activate_delete():
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        # list shows seeded default, active=default, secret masked
        body = c.get("/admin/model-endpoints", headers=_h("admintok")).json()
        assert body["active"] == "default"
        assert "supersecret" not in c.get("/admin/model-endpoints", headers=_h("admintok")).text
        # add a second
        assert c.put("/admin/model-endpoints/claude", headers=_h("admintok"),
                     json={"model": "anthropic/claude", "api_base": "http://b/v1"}).status_code == 200
        # activate it (live switch)
        r = c.post("/admin/model-endpoints/claude/activate", headers=_h("admintok"))
        assert r.status_code == 200 and r.json()["active"] == "claude"
        # delete default (now inactive) ok
        assert c.delete("/admin/model-endpoints/default", headers=_h("admintok")).status_code == 200
        # delete last (claude, active) → 409
        assert c.delete("/admin/model-endpoints/claude", headers=_h("admintok")).status_code == 409
        # activate unknown → 404
        assert c.post("/admin/model-endpoints/nope/activate", headers=_h("admintok")).status_code == 404
    print("OK test_routes_list_put_activate_delete")


if __name__ == "__main__":
    test_first_endpoint_becomes_active()
    test_activate_and_persist()
    test_activate_unknown_raises()
    test_remove_guards()
    test_seed_default_idempotent()
    test_masked_never_leaks_key()
    test_selectable_resolves_active_per_call()
    test_selectable_falls_back_to_default_when_no_active()
    test_selectable_lazy_registry_from_env()
    test_selectable_generate_delegates()
    test_routes_admin_gate()
    test_routes_list_put_activate_delete()
    print("\nall model-endpoint tests passed")
