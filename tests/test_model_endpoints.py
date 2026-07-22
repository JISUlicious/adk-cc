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


# --- missing-api-key bug (the LiteLLM auth failure) -----------------------

class _FakeLlmForKey:
    """Captures the kwargs a real LiteLlm would be built with."""
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


def test_build_litellm_raises_on_missing_key_env():
    # Regression for the silent-drop bug: an endpoint declaring an api_key_env
    # that is NOT set must FAIL LOUD, not build a keyless LiteLlm that then
    # errors with an opaque litellm auth failure downstream.
    os.environ.pop("MISSING_KEY_VAR", None)
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(ModelEndpointConfig(name="ep", model="openai/m",
                 api_base="http://x/v1", api_key_env="MISSING_KEY_VAR"))
        sel = SelectableLlm(registry=r)
        try:
            sel._resolve_delegate()
            assert False, "expected ValueError for missing key env"
        except ValueError as e:
            assert "MISSING_KEY_VAR" in str(e) and "not set" in str(e), e
    print("OK test_build_litellm_raises_on_missing_key_env")


def test_build_litellm_passes_key_when_present():
    os.environ["PRESENT_KEY_VAR"] = "sk-real"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = _reg(tmp)
            r.upsert(ModelEndpointConfig(name="ep", model="openai/m",
                     api_base="http://x/v1", api_key_env="PRESENT_KEY_VAR"))
            sel = SelectableLlm(registry=r)
            sel._build_litellm = lambda cfg: _FakeLlmForKey(  # type: ignore
                model=cfg.model, api_base=cfg.api_base, api_key=cfg.resolve_api_key())
            sel._resolve_delegate()
            assert _FakeLlmForKey.last_kwargs["api_key"] == "sk-real"
    finally:
        os.environ.pop("PRESENT_KEY_VAR", None)
    print("OK test_build_litellm_passes_key_when_present")


def test_keyless_endpoint_allowed():
    # api_key_env="" → intentionally keyless (e.g. local no-auth model). Must
    # build WITHOUT a key and WITHOUT raising.
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(ModelEndpointConfig(name="local", model="openai/m",
                 api_base="http://localhost:8000/v1", api_key_env=""))
        sel = SelectableLlm(registry=r)
        d = sel._resolve_delegate()  # must not raise
        assert d is not None
    print("OK test_keyless_endpoint_allowed")


def test_activate_rejects_missing_key():
    os.environ.pop("ALSO_MISSING", None)
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(_cfg("ok"))  # default api_key_env=ADK_CC_API_KEY (=stub, set)
        r.upsert(ModelEndpointConfig(name="bad", model="openai/m",
                 api_base="http://x/v1", api_key_env="ALSO_MISSING"))
        try:
            r.activate("bad")
            assert False, "expected activate to reject missing-key endpoint"
        except ValueError as e:
            assert "ALSO_MISSING" in str(e), e
        # the good one activates fine
        r.activate("ok")
        assert r.active_name() == "ok"
    print("OK test_activate_rejects_missing_key")


def test_route_activate_missing_key_409():
    from fastapi import FastAPI, HTTPException
    from starlette.testclient import TestClient
    from adk_cc.service.auth import AuthPrincipal, BearerTokenExtractor, make_auth_middleware
    from adk_cc.service.admin_routes import mount_model_admin

    os.environ.pop("BOGUS_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.seed_default(_cfg("default"))  # valid (ADK_CC_API_KEY=stub)
        r.upsert(ModelEndpointConfig(name="bad", model="openai/m",
                 api_base="http://x/v1", api_key_env="BOGUS_KEY"))

        def authorize(request, target):
            auth = getattr(request.state, "adk_cc_auth", None)
            if auth is None or "admin" not in (getattr(auth, "roles", frozenset()) or frozenset()):
                raise HTTPException(403, "need admin")

        app = FastAPI()
        mount_model_admin(app, registry=r, authorize=authorize)
        app.add_middleware(make_auth_middleware(BearerTokenExtractor(
            {"admintok": AuthPrincipal("a", "local", frozenset({"admin"}))})))
        c = TestClient(app)
        h = {"Authorization": "Bearer admintok"}
        # activating the bad endpoint → 409 (not 404, not 500)
        assert c.post("/admin/model-endpoints/bad/activate", headers=h).status_code == 409
        # unknown → still 404
        assert c.post("/admin/model-endpoints/nope/activate", headers=h).status_code == 404
    print("OK test_route_activate_missing_key_409")


# --- inline api keys (actual key on the endpoint, not an env-var name) ----

def test_inline_key_semantics():
    """api_key stored inline: non-empty = the key; "" = explicitly keyless
    (local model servers); None = legacy env-var indirection."""
    inline = ModelEndpointConfig(name="p", model="openai/m", api_base="http://x/v1",
                                 api_key="sk-inline")
    assert inline.requires_key() and inline.api_key_present()
    assert inline.resolve_api_key() == "sk-inline"
    assert inline.key_source() == "inline"

    keyless = ModelEndpointConfig(name="l", model="openai/m",
                                  api_base="http://localhost:1234/v1", api_key="")
    assert not keyless.requires_key() and keyless.api_key_present()
    assert keyless.resolve_api_key() is None
    assert keyless.key_source() == "keyless"

    legacy = ModelEndpointConfig(name="e", model="openai/m", api_base="http://x/v1")
    assert legacy.api_key is None and legacy.key_source() == "env"
    print("OK test_inline_key_semantics")


def test_masked_never_leaks_inline_key():
    m = ModelEndpointConfig(name="p", model="m", api_base="u",
                            api_key="sk-verysecret").masked()
    assert "sk-verysecret" not in str(m)
    assert "api_key" not in m                      # raw field stripped entirely
    assert m["api_key_present"] is True and m["key_source"] == "inline"
    print("OK test_masked_never_leaks_inline_key")


def test_inline_key_activation_and_empty_key_accepted():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(_cfg("seed"))
        # inline key → activates without ANY env var involved
        r.upsert(ModelEndpointConfig(name="inline", model="openai/m",
                 api_base="http://x/v1", api_key="sk-abc"))
        r.activate("inline")
        assert r.active_name() == "inline"
        # EMPTY key → accepted and activatable (local personal model server)
        r.upsert(ModelEndpointConfig(name="local", model="openai/m",
                 api_base="http://localhost:8000/v1", api_key=""))
        r.activate("local")
        assert r.active_name() == "local"
        # registry file holds keys → owner-only perms
        import stat
        mode = stat.S_IMODE(os.stat(os.path.join(tmp, "models.json")).st_mode)
        assert mode == 0o600, oct(mode)
    print("OK test_inline_key_activation_and_empty_key_accepted")


def test_selectable_uses_inline_key_and_recaches_on_change():
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(ModelEndpointConfig(name="p", model="openai/m",
                 api_base="http://x/v1", api_key="sk-one"))
        sel = SelectableLlm(registry=r)
        sel._build_litellm = lambda cfg: _FakeLlmForKey(  # type: ignore
            model=cfg.model, api_base=cfg.api_base, api_key=cfg.resolve_api_key())
        sel._resolve_delegate()
        assert _FakeLlmForKey.last_kwargs["api_key"] == "sk-one"
        # replacing the inline key must REBUILD the delegate (cache keyed on it)
        r.upsert(ModelEndpointConfig(name="p", model="openai/m",
                 api_base="http://x/v1", api_key="sk-two"))
        sel._resolve_delegate()
        assert _FakeLlmForKey.last_kwargs["api_key"] == "sk-two"
    print("OK test_selectable_uses_inline_key_and_recaches_on_change")


def test_route_put_preserves_stored_key_when_omitted():
    """api_key is write-only: a PUT without the field keeps the stored key; a
    PUT with api_key="" explicitly clears it to keyless."""
    with tempfile.TemporaryDirectory() as tmp:
        c, _ = _client(tmp)
        h = {"Authorization": "Bearer admintok"}
        body = {"model": "openai/m", "api_base": "http://x/v1", "api_key": "sk-keep"}
        assert c.put("/admin/model-endpoints/p", headers=h, json=body).status_code == 200
        # update WITHOUT api_key → stored key survives
        assert c.put("/admin/model-endpoints/p", headers=h, json={
            "model": "openai/m2", "api_base": "http://x/v1"}).status_code == 200
        listed = c.get("/admin/model-endpoints", headers=h).json()["endpoints"]
        ep = next(e for e in listed if e["name"] == "p")
        assert ep["model"] == "openai/m2"
        assert ep["api_key_present"] is True and ep["key_source"] == "inline"
        assert "sk-keep" not in str(listed)        # masked in responses
        # explicit empty → keyless
        assert c.put("/admin/model-endpoints/p", headers=h, json={
            "model": "openai/m2", "api_base": "http://x/v1", "api_key": ""}).status_code == 200
        listed = c.get("/admin/model-endpoints", headers=h).json()["endpoints"]
        ep = next(e for e in listed if e["name"] == "p")
        assert ep["key_source"] == "keyless" and ep["api_key_present"] is True
    print("OK test_route_put_preserves_stored_key_when_omitted")



# --- per-session model override (contextvar + plugin) ---------------------

def test_session_override_resolution():
    """Override picks the named endpoint + model WITHOUT touching the global
    active pointer or the endpoint's stored model field."""
    from adk_cc.models.selectable import set_session_model_override
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(ModelEndpointConfig(name="glob", model="openai/global-m",
                 api_base="http://g/v1", api_key="sk-g"))
        r.upsert(ModelEndpointConfig(name="other", model="openai/first",
                 api_base="http://o/v1", api_key="sk-o",
                 models=["openai/first", "openai/second"]))
        r.activate("glob")
        sel = SelectableLlm(registry=r)
        sel._build_litellm = lambda cfg: _FakeLlmForKey(  # type: ignore
            model=cfg.model, api_base=cfg.api_base, api_key=cfg.resolve_api_key())
        try:
            # no override → global active
            set_session_model_override(None)
            d_glob = sel._resolve_delegate()
            assert _FakeLlmForKey.last_kwargs["model"] == "openai/global-m"
            # override → named endpoint + per-session model id
            set_session_model_override(("other", "openai/second"))
            d_over = sel._resolve_delegate()
            assert d_over is not d_glob
            assert _FakeLlmForKey.last_kwargs["model"] == "openai/second"
            assert _FakeLlmForKey.last_kwargs["api_base"] == "http://o/v1"
            assert _FakeLlmForKey.last_kwargs["api_key"] == "sk-o"
            # registry untouched: global active + stored model unchanged
            assert r.active_name() == "glob"
            assert r.get("other").model == "openai/first"
            # deleted endpoint → warn + fall back to the global active
            # delegate (the CACHED one — no rebuild, so compare identity)
            set_session_model_override(("gone", "openai/x"))
            assert sel._resolve_delegate() is d_glob
        finally:
            set_session_model_override(None)
    print("OK test_session_override_resolution")


def test_session_override_task_isolation():
    """Two concurrent tasks with different overrides resolve different
    delegates — the contextvar is task-scoped, and asyncio.to_thread (the real
    call path) preserves it."""
    from adk_cc.models.selectable import set_session_model_override
    with tempfile.TemporaryDirectory() as tmp:
        r = _reg(tmp)
        r.upsert(ModelEndpointConfig(name="a", model="openai/ma",
                 api_base="http://a/v1", api_key="ka"))
        r.upsert(ModelEndpointConfig(name="b", model="openai/mb",
                 api_base="http://b/v1", api_key="kb"))
        r.activate("a")
        sel = SelectableLlm(registry=r)

        async def turn(endpoint, expect_model):
            # mimic the plugin (before_model in the turn's task) then the
            # real resolve path (asyncio.to_thread)
            set_session_model_override((endpoint, ""))
            delegate = await asyncio.to_thread(sel._resolve_delegate)
            got = getattr(delegate, "model", None)
            assert got == expect_model, f"{endpoint}: {got} != {expect_model}"
            return delegate

        async def main():
            d1, d2 = await asyncio.gather(turn("a", "openai/ma"), turn("b", "openai/mb"))
            assert d1 is not d2, "distinct endpoints must get distinct cached delegates"

        asyncio.run(main())
    print("OK test_session_override_task_isolation")


def test_model_session_plugin_sets_and_clears():
    from adk_cc.models import selectable as S
    from adk_cc.plugins.model_session import ModelSessionPlugin

    class _Ctx:  # minimal CallbackContext stand-in
        def __init__(self, state):
            self.state = state

    plug = ModelSessionPlugin()

    async def main():
        # pinned session → override set
        await plug.before_model_callback(
            callback_context=_Ctx({"model_endpoint": "prov", "model_id": "openai/x"}),
            llm_request=object())
        assert S._SESSION_MODEL.get() == ("prov", "openai/x")
        # unpinned session (same task!) → override CLEARED, not stale
        await plug.before_model_callback(
            callback_context=_Ctx({}), llm_request=object())
        assert S._SESSION_MODEL.get() is None
        # endpoint set, model empty → endpoint's own model (empty id)
        await plug.before_model_callback(
            callback_context=_Ctx({"model_endpoint": "prov"}), llm_request=object())
        assert S._SESSION_MODEL.get() == ("prov", "")
        # a broken state object must not raise or leave a stale pin
        class _Bad:
            @property
            def state(self):
                raise RuntimeError("boom")
        await plug.before_model_callback(callback_context=_Bad(), llm_request=object())
        assert S._SESSION_MODEL.get() is None
    asyncio.run(main())
    print("OK test_model_session_plugin_sets_and_clears")



# --- rate-limit retry -----------------------------------------------------

class _RL(Exception):
    """Rate-limit stand-in: matched structurally via status_code == 429."""
    status_code = 429


class _FlakyLlm:
    """Delegate that raises `exc` for the first `failures` calls, then streams.
    `fail_after_yield` instead yields one chunk and THEN raises (mid-stream)."""
    def __init__(self, failures=0, exc=None, fail_after_yield=False):
        self.calls = 0
        self.failures = failures
        self.exc = exc or _RL("429 too many requests")
        self.fail_after_yield = fail_after_yield
        self.model = "openai/flaky"

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.fail_after_yield:
            yield "partial"
            raise self.exc
        if self.calls <= self.failures:
            raise self.exc
        yield "ok"


def _retry_sel(tmp, fake):
    r = _reg(tmp)
    r.upsert(ModelEndpointConfig(name="p", model="openai/flaky",
             api_base="http://x/v1", api_key="k"))
    sel = SelectableLlm(registry=r)
    sel._build_litellm = lambda cfg: fake  # type: ignore
    return sel


def test_retry_recovers_from_pre_stream_429():
    from adk_cc.models import selectable as S
    sleeps = []
    async def fake_sleep(d): sleeps.append(d)
    orig = S._retry_sleep
    S._retry_sleep = fake_sleep
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FlakyLlm(failures=2)
            sel = _retry_sel(tmp, fake)
            async def main():
                return [x async for x in sel.generate_content_async(object())]
            out = asyncio.run(main())
            assert out == ["ok"], out
            assert fake.calls == 3, fake.calls          # 2 failures + 1 success
            assert len(sleeps) == 2, sleeps
            # schedule: base 5s doubling, jitter 0-25% (env unset -> defaults)
            assert 5.0 <= sleeps[0] <= 6.25 and 10.0 <= sleeps[1] <= 12.5, sleeps
    finally:
        S._retry_sleep = orig
    print("OK test_retry_recovers_from_pre_stream_429")


def test_retry_never_after_first_chunk():
    from adk_cc.models import selectable as S
    sleeps = []
    async def fake_sleep(d): sleeps.append(d)
    orig = S._retry_sleep
    S._retry_sleep = fake_sleep
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = _FlakyLlm(fail_after_yield=True)
            sel = _retry_sel(tmp, fake)
            got = []
            async def main():
                async for x in sel.generate_content_async(object()):
                    got.append(x)
            try:
                asyncio.run(main())
                assert False, "expected the mid-stream 429 to raise"
            except _RL:
                pass
            assert got == ["partial"] and fake.calls == 1 and sleeps == []
    finally:
        S._retry_sleep = orig
    print("OK test_retry_never_after_first_chunk")


def test_retry_only_rate_limits_and_exhaustion():
    from adk_cc.models import selectable as S
    sleeps = []
    async def fake_sleep(d): sleeps.append(d)
    orig = S._retry_sleep
    S._retry_sleep = fake_sleep
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # non-429 -> immediate raise, no sleeps
            fake = _FlakyLlm(failures=1, exc=ValueError("bad model id"))
            sel = _retry_sel(tmp, fake)
            async def run(s):
                return [x async for x in s.generate_content_async(object())]
            try:
                asyncio.run(run(sel)); assert False
            except ValueError:
                pass
            assert fake.calls == 1 and sleeps == []

            # exhaustion: fails forever -> initial + RETRIES attempts, then raises
            os.environ["ADK_CC_MODEL_RETRIES"] = "2"
            try:
                fake2 = _FlakyLlm(failures=99)
                sel2 = _retry_sel(tmp, fake2)
                try:
                    asyncio.run(run(sel2)); assert False
                except _RL:
                    pass
                assert fake2.calls == 3, fake2.calls    # 1 + 2 retries
                assert len(sleeps) == 2

                # RETRIES=0 disables entirely
                os.environ["ADK_CC_MODEL_RETRIES"] = "0"
                fake3 = _FlakyLlm(failures=1)
                sel3 = _retry_sel(tmp, fake3)
                try:
                    asyncio.run(run(sel3)); assert False
                except _RL:
                    pass
                assert fake3.calls == 1
            finally:
                os.environ.pop("ADK_CC_MODEL_RETRIES", None)
    finally:
        S._retry_sleep = orig
    print("OK test_retry_only_rate_limits_and_exhaustion")


def test_retry_honors_retry_after_hint():
    from adk_cc.models import selectable as S
    # provider hint is a FLOOR over the computed backoff, capped at 60s
    class _Hinted(_RL):
        retry_after = 42
    d = S._retry_delay_s(_Hinted(), attempt=1, base=5.0)
    assert 42.0 <= d <= 60.0, d
    class _Huge(_RL):
        retry_after = 999
    assert S._retry_delay_s(_Huge(), attempt=1, base=5.0) == 60.0
    # litellm/openai class-name match (no status_code attr needed)
    class RateLimitError(Exception):
        pass
    assert S._is_rate_limited(RateLimitError())
    assert not S._is_rate_limited(ValueError())
    print("OK test_retry_honors_retry_after_hint")



def test_429_classification_and_ladders():
    """F2a: three 429 classes get three responses (see models/rate_limit.py)."""
    import time as _time
    from adk_cc.models import selectable as S
    from adk_cc.models.rate_limit import classify_429, describe_quota

    class _H(dict):
        pass

    def _err(msg="429", headers=None):
        e = _RL(msg)
        if headers is not None:
            e.response = type("R", (), {"headers": _H(headers)})()
        return e

    # upstream: body sniff (today's gemma:free failure text)
    k, _ = classify_429(_err("google/gemma-4-31b-it:free is temporarily rate-limited upstream. Please retry shortly"))
    assert k == "upstream", k
    # quota: reset hours away (epoch seconds)
    k, hint = classify_429(_err("Rate limit exceeded", {"x-ratelimit-reset": str(_time.time() + 5 * 3600)}))
    assert k == "quota" and hint and hint > 3600, (k, hint)
    assert "resets in ~" in describe_quota(hint)
    # burst: plain 429, near reset
    k, _ = classify_429(_err("Rate limit exceeded", {"retry-after": "7"}))
    assert k == "burst", k

    # ladders: upstream is ~6x slower and caps at 120s
    b1 = S._retry_delay_s(_err("x"), 1, 5.0, "burst")
    u1 = S._retry_delay_s(_err("x"), 1, 5.0, "upstream")
    assert 5.0 <= b1 <= 6.25 and 30.0 <= u1 <= 37.5, (b1, u1)
    u9 = S._retry_delay_s(_err("x"), 9, 5.0, "upstream")
    assert u9 <= 120.0, u9

    # quota → fail-fast from the retry envelope with the actionable message
    from adk_cc.models.selectable import set_session_model_override
    with tempfile.TemporaryDirectory() as tmp:
        fake = _FlakyLlm(failures=99,
                         exc=_err("Rate limit exceeded",
                                  {"x-ratelimit-reset": str(_time.time() + 4 * 3600)}))
        sel = _retry_sel(tmp, fake)
        async def run():
            return [x async for x in sel.generate_content_async(object())]
        try:
            asyncio.run(run()); assert False, "expected fail-fast"
        except RuntimeError as e:
            assert "quota exhausted" in str(e), e
            assert fake.calls == 1, fake.calls   # zero retries burned
    print("OK test_429_classification_and_ladders")



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
    # missing-api-key regression (the LiteLLM auth bug)
    test_build_litellm_raises_on_missing_key_env()
    test_build_litellm_passes_key_when_present()
    test_keyless_endpoint_allowed()
    test_activate_rejects_missing_key()
    test_route_activate_missing_key_409()
    test_inline_key_semantics()
    test_masked_never_leaks_inline_key()
    test_inline_key_activation_and_empty_key_accepted()
    test_selectable_uses_inline_key_and_recaches_on_change()
    test_route_put_preserves_stored_key_when_omitted()
    test_session_override_resolution()
    test_session_override_task_isolation()
    test_model_session_plugin_sets_and_clears()
    test_retry_recovers_from_pre_stream_429()
    test_retry_never_after_first_chunk()
    test_retry_only_rate_limits_and_exhaustion()
    test_retry_honors_retry_after_hint()
    test_429_classification_and_ladders()
    print("\nall model-endpoint tests passed")
