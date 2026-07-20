"""Unit tests for the central env-var schema (agents/adk_cc/config.py).

Phase 1 is a faithful MIRROR of current behavior, so the load-bearing checks
are: (a) declared defaults match the current inline defaults (locked here so a
drift is caught), (b) parsing is correct, (c) required detection (incl.
conditional) works, (d) the generated .env.example puts required vars
uncommented and everything else commented, and (e) the schema is well-formed
(unique ADK_CC_ names).

Run: `uv run python tests/test_config_schema.py`
"""

from __future__ import annotations

import os

os.environ.setdefault("ADK_CC_API_KEY", "sk-dummy-for-tests")

from adk_cc.config import schema as C  # noqa: E402


def test_schema_wellformed():
    names = [v.name for v in C.FIELDS]
    assert len(names) == len(set(names)), "duplicate var names"
    assert all(n.startswith("ADK_CC_") for n in names)
    # required-but-unconditional vs conditional are distinguishable
    assert any(v.tier is C.Tier.REQUIRED and v.required_if is None for v in C.FIELDS)
    print("OK schema_wellformed")


def test_defaults_mirror_current_behavior():
    """These MUST equal the current inline defaults in the code. If a default
    changes in code, change it here too — that's the point (single source)."""
    r = C.resolve({})  # empty env → all defaults
    expected = {
        "ADK_CC_API_KEY": None,               # required, no default
        "ADK_CC_API_BASE": "http://localhost:18000/v1",
        "ADK_CC_MODEL": "openai/Qwen3.6-35B-A3B-UD-MLX-4bit",
        "ADK_CC_MAX_OUTPUT_TOKENS": 8192,
        "ADK_CC_MAX_OUTPUT_TOKENS_ESCALATED": 32768,
        "ADK_CC_MODEL_MAX_RPM": None,          # off
        "ADK_CC_PERMISSION_MODE": "bypassPermissions",
        "ADK_CC_SANDBOX_BACKEND": "noop",
        "ADK_CC_SANDBOX_NETWORK": True,
        "ADK_CC_SANDBOX_REQUIRE": False,
        "ADK_CC_MEMORY": False,
        "ADK_CC_WIKI": False,
        "ADK_CC_AUTH_PASSWORD": False,
        "ADK_CC_LOG_LEVEL": "INFO",
        "ADK_CC_LOG_FORMAT": "text",
        "ADK_CC_TOLERANT_TOOL_JSON": True,      # default-on kill switch
        "ADK_CC_CHECKPOINT": True,
        "ADK_CC_WEB_FETCH_MODE": "open",
        "ADK_CC_SKIP_DOTENV": False,
    }
    for name, want in expected.items():
        assert r[name] == want, f"{name}: default {r[name]!r} != expected {want!r}"
    print("OK defaults_mirror_current_behavior")


def test_parsing():
    env = {
        "ADK_CC_API_KEY": "sk-x",
        "ADK_CC_MAX_OUTPUT_TOKENS": "0",         # 0 = uncapped (still int 0)
        "ADK_CC_MODEL_MAX_RPM": "7.5",
        "ADK_CC_TOLERANT_TOOL_JSON": "0",        # disable the default-on feature
        "ADK_CC_MEMORY": "1",                     # enable the default-off feature
        "ADK_CC_SANDBOX_ENV_PASSTHROUGH": "A, B ,C",
    }
    r = C.resolve(env)
    assert r["ADK_CC_API_KEY"] == "sk-x"
    assert r["ADK_CC_MAX_OUTPUT_TOKENS"] == 0
    assert r["ADK_CC_MODEL_MAX_RPM"] == 7.5
    assert r["ADK_CC_TOLERANT_TOOL_JSON"] is False
    assert r["ADK_CC_MEMORY"] is True
    assert r["ADK_CC_SANDBOX_ENV_PASSTHROUGH"] == ("A", "B", "C")
    # garbage int → falls back to default, and check() warns
    assert C.resolve({"ADK_CC_MAX_OUTPUT_TOKENS": "abc"})["ADK_CC_MAX_OUTPUT_TOKENS"] == 8192
    print("OK parsing")


def test_check_required_and_conditional():
    # API_KEY missing → hard error.
    errors, _ = C.check({})
    assert any("ADK_CC_API_KEY" in e for e in errors), errors

    # With API_KEY + backend=daytona → daytona creds become conditionally required (warnings).
    _, warnings = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_SANDBOX_BACKEND": "daytona"})
    assert any("ADK_CC_DAYTONA_API_URL" in w for w in warnings), warnings
    assert any("ADK_CC_DAYTONA_API_KEY" in w for w in warnings), warnings

    # backend=noop → daytona creds NOT required (no warning about them).
    e2, w2 = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_DESKTOP": "1"})
    assert not any("DAYTONA" in m for m in e2 + w2), (e2, w2)
    print("OK check_required_and_conditional")


def test_gen_env_shape():
    text = C.render_env_example(C.Profile.ALL)
    # Required unconditional var is uncommented; advanced var is commented.
    assert "\nADK_CC_API_KEY=" in text, "required var must be uncommented"
    assert "# ADK_CC_MAX_OUTPUT_TOKENS=" in text, "advanced var must be commented"
    # Quickstart precedes Reference.
    assert text.index("QUICKSTART") < text.index("REFERENCE")
    # Desktop profile hides web-only auth vars.
    dtext = C.render_env_example(C.Profile.DESKTOP)
    assert "ADK_CC_JWT_JWKS_URL" not in dtext, "web-only var leaked into desktop profile"
    assert "ADK_CC_API_KEY=" in dtext
    print("OK gen_env_shape")


def test_effective_masks_secrets():
    out = C.render_effective({"ADK_CC_API_KEY": "sk-secret"}, show_secrets=False)
    assert "sk-secret" not in out and "ADK_CC_API_KEY = ••••" in out
    assert "sk-secret" in C.render_effective({"ADK_CC_API_KEY": "sk-secret"}, show_secrets=True)
    print("OK effective_masks_secrets")


def main():
    test_schema_wellformed()
    test_defaults_mirror_current_behavior()
    test_parsing()
    test_check_required_and_conditional()
    test_gen_env_shape()
    test_effective_masks_secrets()
    print("\nall config-schema tests passed")


if __name__ == "__main__":
    main()
