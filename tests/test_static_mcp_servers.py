"""Tests for the multi-static MCP loader (tools/mcp.load_static_mcp_servers).

Static servers are declared in a JSON-array file (ADK_CC_MCP_SERVERS_FILE)
of McpServerConfig objects. The loader builds one toolset per valid entry,
is fault-isolated (bad file / bad entry never crashes boot), reads
credential_key as an ENV VAR name for the bearer token, and dedups against
already-wired names.

Construction does NOT open MCP connections (McpToolset connects lazily at
get_tools), so we assert on the built toolsets + their prefixes, not on
live servers.

Hand-rolled (no pytest), run with the venv python.
"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.mcp import (
    McpServerConfig,
    _ArtifactSavingMcpToolset,
    load_static_mcp_servers,
    toolset_for_static_config,
)


def _write(tmp, obj):
    """Write obj as JSON to a temp file, return its path."""
    path = os.path.join(tmp, "mcp.json")
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)          # raw (for malformed-JSON tests)
        else:
            json.dump(obj, f)
    return path


def _prefix(ts):
    """Recover the mcp__<name>__ prefix from a built toolset (inner McpToolset
    for the artifact wrapper, else the toolset itself)."""
    inner = getattr(ts, "_inner", ts)
    return getattr(inner, "tool_name_prefix", None)


def _server(name, transport="stdio", url="python x.py", **kw):
    return {"server_name": name, "transport": transport, "url": url, **kw}


# --- happy path -----------------------------------------------------------

def test_two_servers_two_toolsets():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            _server("github", "http", "https://api.github.com/mcp"),
            _server("csv", "stdio", "python tests/fixtures/csv_mcp_server.py"),
        ])
        out = load_static_mcp_servers(path)
    assert len(out) == 2, out
    prefixes = sorted(_prefix(t) for t in out)
    assert prefixes == ["mcp__csv__", "mcp__github__"], prefixes
    print("OK test_two_servers_two_toolsets")


def test_save_resources_wraps_toolset():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [_server("gh", "http", "https://x/mcp",
                                    save_resources_as_artifacts=True)])
        out = load_static_mcp_servers(path)
    assert len(out) == 1
    assert isinstance(out[0], _ArtifactSavingMcpToolset), type(out[0])
    print("OK test_save_resources_wraps_toolset")


# --- fault isolation ------------------------------------------------------

def test_missing_file_returns_empty():
    assert load_static_mcp_servers("/no/such/file.json") == []
    print("OK test_missing_file_returns_empty")


def test_unset_returns_empty():
    os.environ.pop("ADK_CC_MCP_SERVERS_FILE", None)
    assert load_static_mcp_servers() == []
    print("OK test_unset_returns_empty")


def test_malformed_json_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, "{ not valid json")
        assert load_static_mcp_servers(path) == []
    print("OK test_malformed_json_returns_empty")


def test_non_array_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, {"server_name": "x"})  # object, not array
        assert load_static_mcp_servers(path) == []
    print("OK test_non_array_returns_empty")


def test_one_bad_entry_others_load():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            _server("good1", "http", "https://a/mcp"),
            {"server_name": "bad"},                 # missing transport/url
            _server("good2", "stdio", "python b.py"),
        ])
        out = load_static_mcp_servers(path)
    names = sorted(_prefix(t) for t in out)
    assert names == ["mcp__good1__", "mcp__good2__"], names
    print("OK test_one_bad_entry_others_load")


def test_duplicate_name_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            _server("dup", "http", "https://a/mcp"),
            _server("dup", "stdio", "python b.py"),  # same name → dropped
        ])
        out = load_static_mcp_servers(path)
    assert len(out) == 1, out
    print("OK test_duplicate_name_skipped")


def test_exclude_names_drops_collision():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(tmp, [
            _server("mcp", "http", "https://a/mcp"),   # collides with single env server
            _server("extra", "http", "https://b/mcp"),
        ])
        out = load_static_mcp_servers(path, exclude_names=frozenset({"mcp"}))
    prefixes = sorted(_prefix(t) for t in out)
    assert prefixes == ["mcp__extra__"], prefixes
    print("OK test_exclude_names_drops_collision")


# --- static auth = env var ------------------------------------------------

def test_credential_key_reads_env_var():
    os.environ["MY_MCP_TOKEN"] = "secret-abc"
    try:
        cfg = McpServerConfig(server_name="auth", transport="http",
                              url="https://x/mcp", credential_key="MY_MCP_TOKEN")
        ts = toolset_for_static_config(cfg)
        inner = getattr(ts, "_inner", ts)
        # Assert on what the loader ACTUALLY built: the env var's value was
        # folded into the connection params' Authorization header.
        params = inner._connection_params
        assert getattr(params, "headers", None) == {
            "Authorization": "Bearer secret-abc"
        }, getattr(params, "headers", None)
    finally:
        os.environ.pop("MY_MCP_TOKEN", None)
    print("OK test_credential_key_reads_env_var")


def test_credential_key_missing_env_no_auth():
    os.environ.pop("ABSENT_TOKEN", None)
    cfg = McpServerConfig(server_name="auth", transport="http",
                          url="https://x/mcp", credential_key="ABSENT_TOKEN")
    # Should still build a toolset (unauthenticated) without raising.
    ts = toolset_for_static_config(cfg)
    assert ts is not None
    print("OK test_credential_key_missing_env_no_auth")


if __name__ == "__main__":
    test_two_servers_two_toolsets()
    test_save_resources_wraps_toolset()
    test_missing_file_returns_empty()
    test_unset_returns_empty()
    test_malformed_json_returns_empty()
    test_non_array_returns_empty()
    test_one_bad_entry_others_load()
    test_duplicate_name_skipped()
    test_exclude_names_drops_collision()
    test_credential_key_reads_env_var()
    test_credential_key_missing_env_no_auth()
    print("\nall static-mcp-servers tests passed")
