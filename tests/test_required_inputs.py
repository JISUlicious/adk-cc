"""Phase 3: skill secret declaration registry + least-privilege injection.

Covers: parsing metadata["x-adk-cc/secrets"] (JSON/dict/comma/malformed),
discovery from a real SKILL.md, and that _runtime_env() injects ONLY declared
keys when declarations exist (allowlist), all when none do (fallback).

Model-free. Run: .venv/bin/python tests/test_required_inputs.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.credentials.impls import InMemoryCredentialProvider  # noqa: E402
from adk_cc.credentials.required_inputs import (  # noqa: E402
    RequiredInput,
    _parse_declaration,
    declared_secret_keys,
    discover_groups,
    required_inputs,
)
from adk_cc.sandbox.backends.noop_backend import NoopBackend  # noqa: E402


def test_parse_declaration_forms():
    # JSON list of dicts
    out = _parse_declaration(
        '[{"id":"A","description":"d","secret":true},{"id":"B"}]', source="s"
    )
    assert [r.id for r in out] == ["A", "B"]
    assert out[0].description == "d" and out[0].source == "s"
    # JSON list of strings
    assert [r.id for r in _parse_declaration('["X","Y"]', source="s")] == ["X", "Y"]
    # plain comma list
    assert [r.id for r in _parse_declaration("P, Q ,R", source="s")] == ["P", "Q", "R"]
    # single dict
    assert [r.id for r in _parse_declaration('{"id":"Z"}', source="s")] == ["Z"]
    # malformed JSON -> [] (never raises)
    assert _parse_declaration("[not json", source="s") == []
    # empty / None
    assert _parse_declaration("", source="s") == []
    assert _parse_declaration(None, source="s") == []


def test_discovery_from_skill_md():
    parent = tempfile.mkdtemp(prefix="reqinputs-")
    skill_dir = Path(parent) / "test-secret-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: test-secret-skill\n"
        "description: A test skill that needs a declared secret to do its job.\n"
        "metadata:\n"
        '  x-adk-cc/secrets: \'[{"id":"DECLARED_TOKEN","description":"A token","secret":true}]\'\n'
        "---\n\nBody.\n"
    )
    old = os.environ.get("ADK_CC_SKILLS_DIR")
    os.environ["ADK_CC_SKILLS_DIR"] = parent
    try:
        keys = declared_secret_keys(refresh=True)
        assert "DECLARED_TOKEN" in keys, keys
        ris = required_inputs(refresh=True)
        ri = next(r for r in ris if r.id == "DECLARED_TOKEN")
        assert ri.description == "A token" and ri.source.startswith("skill:")
    finally:
        if old is None:
            os.environ.pop("ADK_CC_SKILLS_DIR", None)
        else:
            os.environ["ADK_CC_SKILLS_DIR"] = old
        declared_secret_keys(refresh=True)  # reset cache for other tests


def test_discover_groups_by_skill():
    parent = tempfile.mkdtemp(prefix="groups-")
    sd = Path(parent) / "g-skill"
    sd.mkdir()
    (sd / "SKILL.md").write_text(
        "---\nname: g-skill\n"
        "description: A grouping test skill that requires a token to call out.\n"
        "metadata:\n"
        '  x-adk-cc/secrets: \'[{"id":"G_TOKEN","description":"grp token"}]\'\n'
        "---\n\nBody.\n"
    )
    old = os.environ.get("ADK_CC_SKILLS_DIR")
    os.environ["ADK_CC_SKILLS_DIR"] = parent
    try:
        declared_secret_keys(refresh=True)
        groups = asyncio.run(discover_groups("acme"))
        g = next((x for x in groups if x.kind == "skill" and x.name == "g-skill"), None)
        assert g is not None, groups
        assert [i.id for i in g.inputs] == ["G_TOKEN"], g
    finally:
        if old is None:
            os.environ.pop("ADK_CC_SKILLS_DIR", None)
        else:
            os.environ["ADK_CC_SKILLS_DIR"] = old
        declared_secret_keys(refresh=True)


def test_runtime_env_allowlist_filters_to_declared():
    creds = InMemoryCredentialProvider(shared=False)
    asyncio.run(creds.put(tenant_id="acme", key="DECLARED", value="d", user_id="alice"))
    asyncio.run(creds.put(tenant_id="acme", key="OTHER", value="o", user_id="alice"))

    # declared allowlist -> only DECLARED injected
    b = NoopBackend()
    b.configure_runtime_env(
        credentials=creds, tenant_id="acme", user_id="alice",
        declared_keys={"DECLARED"}, ttl_s=0.0,
    )
    env = asyncio.run(b._runtime_env())
    assert env.get("DECLARED") == "d"
    assert "OTHER" not in env, f"undeclared secret leaked into env: {env}"

    # no declarations -> fallback injects all the user's secrets
    b2 = NoopBackend()
    b2.configure_runtime_env(
        credentials=creds, tenant_id="acme", user_id="alice",
        declared_keys=set(), ttl_s=0.0,
    )
    env2 = asyncio.run(b2._runtime_env())
    assert env2.get("DECLARED") == "d" and env2.get("OTHER") == "o", env2


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
    print("\nall required-inputs tests passed" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
