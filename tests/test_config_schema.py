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
    # API_KEY missing → WARNING, not error: the app deliberately boots keyless
    # (desktop first-run enters the key in the UI; agent.py warns only), so
    # `config check` as a CI gate must not fail a supported configuration.
    errors, warnings = C.check({})
    assert not any("ADK_CC_API_KEY" in e for e in errors), errors
    assert any("ADK_CC_API_KEY" in w for w in warnings), warnings

    # With API_KEY + backend=daytona → daytona creds become conditionally required (warnings).
    _, warnings = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_SANDBOX_BACKEND": "daytona"})
    assert any("ADK_CC_DAYTONA_API_URL" in w for w in warnings), warnings
    assert any("ADK_CC_DAYTONA_API_KEY" in w for w in warnings), warnings

    # backend=noop → daytona creds NOT required (no warning about them).
    e2, w2 = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_SANDBOX_BACKEND": "noop", "ADK_CC_DESKTOP": "1"})
    assert not any("DAYTONA" in m for m in e2 + w2), (e2, w2)
    print("OK check_required_and_conditional")


def test_enum_choices_and_rules():
    """Enum vars reject out-of-choice values (error); cross-var rules flag
    dangerous/contradictory combinations."""
    base = {"ADK_CC_API_KEY": "x", "ADK_CC_DESKTOP": "1"}

    # bad enum value → error naming the var; valid value → no such error.
    e, _ = C.check({**base, "ADK_CC_SANDBOX_BACKEND": "Docker"})  # capital D not a choice
    assert any("ADK_CC_SANDBOX_BACKEND" in m and "Docker" in m for m in e), e
    e2, _ = C.check({**base, "ADK_CC_SANDBOX_BACKEND": "docker"})
    assert not any("ADK_CC_SANDBOX_BACKEND" in m for m in e2), e2

    # every enum var actually carries choices (guards against forgetting one).
    enum_names = {
        "ADK_CC_PERMISSION_MODE", "ADK_CC_SANDBOX_BACKEND", "ADK_CC_LOG_FORMAT",
        "ADK_CC_WEB_FETCH_MODE", "ADK_CC_CREDENTIAL_PROVIDER", "ADK_CC_TENANCY_MODE",
        "ADK_CC_SANDBOX_MODE", "ADK_CC_SANDBOX_RUNTIME", "ADK_CC_MCP_TRANSPORT",
    }
    for n in enum_names:
        assert C.BY_NAME[n].choices, f"{n} should declare choices"
        assert C.BY_NAME[n].default in C.BY_NAME[n].choices, f"{n} default not in its own choices"

    # cross-var rule: compaction threshold without retention → error.
    e3, _ = C.check({**base, "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "1000"})
    assert any("COMPACTION" in m for m in e3), e3
    # both set → no compaction error.
    e4, _ = C.check({**base, "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "1000",
                     "ADK_CC_COMPACTION_EVENT_RETENTION": "20"})
    assert not any("COMPACTION" in m for m in e4), e4

    # dangerous combos → warnings (not errors).
    _, w = C.check({**base, "ADK_CC_WEB_FETCH_ALLOW_PRIVATE": "1"})
    assert any("SSRF" in m for m in w), w
    _, w2 = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_ALLOW_NO_AUTH": "1"})  # web (no DESKTOP)
    assert any("NO auth" in m for m in w2), w2
    print("OK enum_choices_and_rules")


def test_check_trustworthy():
    """Phase-B review fixes: the check's model must MATCH the runtime."""
    base = {"ADK_CC_API_KEY": "x", "ADK_CC_DESKTOP": "1"}

    # env_bool is the one canonical bool read (unset→default; token semantics).
    assert C.env_bool("ADK_CC_NOPE", True, environ={}) is True
    assert C.env_bool("X", environ={"X": "true"}) is True
    assert C.env_bool("X", True, environ={"X": "off"}) is False

    # resolve(): out-of-choices enum falls back to the DEFAULT (like bad ints),
    # while check() still errors from the raw value.
    r = C.resolve({"ADK_CC_PERMISSION_MODE": "plany"})
    assert r["ADK_CC_PERMISSION_MODE"] == "bypassPermissions", r["ADK_CC_PERMISSION_MODE"]
    e, _ = C.check({**base, "ADK_CC_PERMISSION_MODE": "plany"})
    assert any("ADK_CC_PERMISSION_MODE" in m for m in e), e

    # Compaction rule mirrors agent.py's real gate:
    # RETENTION alone → ignored (warn, NOT error — code silently disables).
    e2, w2 = C.check({**base, "ADK_CC_COMPACTION_EVENT_RETENTION": "20"})
    assert not any("COMPACTION" in m for m in e2), e2
    assert any("ignored" in m for m in w2 if "COMPACTION" in m), w2
    # THRESHOLD without RETENTION → error (agent.py raises).
    e3, _ = C.check({**base, "ADK_CC_COMPACTION_TOKEN_THRESHOLD": "1000"})
    assert any("COMPACTION" in m for m in e3), e3
    # INTERVAL+RETENTION without THRESHOLD → error (enable via interval, unpaired).
    e4, _ = C.check({**base, "ADK_CC_COMPACTION_INTERVAL": "5",
                     "ADK_CC_COMPACTION_EVENT_RETENTION": "20"})
    assert any("COMPACTION" in m for m in e4), e4

    # AUTH_TOKENS silently dead under a higher-priority mode → warned.
    _, w5 = C.check({"ADK_CC_API_KEY": "x", "ADK_CC_AUTH_TOKENS": "t=u:t",
                     "ADK_CC_JWT_JWKS_URL": "https://idp/jwks",
                     "ADK_CC_JWT_ISSUER": "i", "ADK_CC_JWT_AUDIENCE": "a"})
    assert any("ADK_CC_AUTH_TOKENS" in m for m in w5), w5

    # as_csv preserves host:port; as_csv_colon still splits on colon.
    assert C.as_csv("example.com:8443, b.com") == ("example.com:8443", "b.com")
    assert C.as_csv_colon("/a:/b,/c") == ("/a", "/b", "/c")
    print("OK check_trustworthy")


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


def test_coverage_complete():
    """Every ADK_CC_* var read anywhere in agents/ must be in the schema — this
    is the guard that keeps the generated docs complete (a new env var forces a
    schema row). Prefix-only artifacts (grep fragments) are excluded."""
    import re
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        ["grep", "-rhoE", "--include=*.py", "ADK_CC_[A-Z0-9_]+", os.path.join(repo, "agents")],
        capture_output=True, text=True,
    ).stdout
    tok = re.compile(r"^ADK_CC_[A-Z0-9_]+$")
    used = {n for n in out.split() if tok.match(n) and not n.endswith("_")}  # drop prefix fragments
    # REMOVED/DEPRECATED registry names count as covered: they appear in the
    # tree (registry keys; a deprecated alias read) precisely so check() can
    # warn about them — they must not need live Var rows.
    schema = set(C.BY_NAME) | set(C.REMOVED) | set(C.DEPRECATED)
    missing = sorted(used - schema)
    assert not missing, f"{len(missing)} env var(s) read but missing from the schema: {missing}"
    print(f"OK coverage_complete ({len(schema)} vars, all read-sites covered)")


def test_env_example_in_sync():
    """The committed .env.example must equal the generator's output — this
    stops the docs from ever drifting from the schema. If this fails, run
    `python -m adk_cc.config gen-env --out .env.example`."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, ".env.example")
    with open(path, encoding="utf-8") as f:
        committed = f.read()
    generated = C.render_env_example(C.Profile.ALL)
    assert committed == generated, (
        ".env.example is out of sync with the schema — run "
        "`python -m adk_cc.config gen-env --out .env.example`"
    )
    print("OK env_example_in_sync")


def main():
    test_schema_wellformed()
    test_coverage_complete()
    test_env_example_in_sync()
    test_defaults_mirror_current_behavior()
    test_parsing()
    test_check_required_and_conditional()
    test_check_trustworthy()
    test_enum_choices_and_rules()
    test_gen_env_shape()
    test_effective_masks_secrets()
    print("\nall config-schema tests passed")


if __name__ == "__main__":
    main()
