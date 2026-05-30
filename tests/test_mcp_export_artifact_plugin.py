"""Tests for McpExportArtifactPlugin (Pattern C).

Drives after_tool_callback with fake MCP CallToolResult dicts and a fake
ToolContext, asserting the gating, save calls, and result rewriting.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import base64
import os
import types as pytypes

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
# Enable the plugin for tests that exercise the active path; individual
# tests toggle plugin._enabled directly for the disabled-gate case.
os.environ["ADK_CC_MCP_AUTOSAVE_EXPORTS"] = "1"

from adk_cc.plugins.mcp_export_artifact import McpExportArtifactPlugin


# --- fakes ----------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.saved = []

    async def save_artifact(self, filename, part):
        self.saved.append((filename, part))
        return len(self.saved) - 1

    # _invocation_context unused for session scope (ctx.save_artifact path)
    _invocation_context = pytypes.SimpleNamespace(
        artifact_service=object(), app_name="adk_cc", user_id="alice"
    )


class _Tool:
    def __init__(self, name):
        self.name = name


def _embedded(text=None, blob=None, mime="text/csv", audience=("user",)):
    res = {"uri": "export://orders", "mimeType": mime}
    if text is not None:
        res["text"] = text
    if blob is not None:
        res["blob"] = base64.b64encode(blob).decode()
    item = {"type": "resource", "resource": res}
    if audience is not None:
        item["annotations"] = {"audience": list(audience)}
    return item


def _link(uri, name="orders.csv", mime="text/csv", audience=("user",)):
    item = {"type": "resource_link", "uri": uri, "name": name, "mimeType": mime}
    if audience is not None:
        item["annotations"] = {"audience": list(audience)}
    return item


def _result(content, structured=None):
    r = {"content": content, "isError": False}
    if structured is not None:
        r["structuredContent"] = structured
    return r


def _run(coro):
    return asyncio.run(coro)


def _call(plugin, tool_name, result, ctx=None):
    ctx = ctx or _FakeCtx()
    out = _run(plugin.after_tool_callback(
        tool=_Tool(tool_name), tool_args={}, tool_context=ctx, result=result))
    return out, ctx


# --- tests ----------------------------------------------------------------

def test_disabled_gate_noops():
    p = McpExportArtifactPlugin()
    p._enabled = False
    out, ctx = _call(p, "mcp__db__query", _result([_embedded(text="a,b\n1,2")]))
    assert out is None and not ctx.saved
    print("OK test_disabled_gate_noops")


def test_non_mcp_tool_noops():
    p = McpExportArtifactPlugin()
    out, ctx = _call(p, "read_file", _result([_embedded(text="x")]))
    assert out is None and not ctx.saved
    print("OK test_non_mcp_tool_noops")


def test_embedded_text_saved_and_stripped():
    p = McpExportArtifactPlugin()
    csv = "id,name\n1,alice\n2,bob\n"
    out, ctx = _call(p, "mcp__db__export", _result([_embedded(text=csv)]))
    assert ctx.saved, "should have saved"
    assert out is not None and "_artifacts" in out
    assert out["_artifacts"][0]["bytes"] == len(csv.encode())
    # embedded inline bytes stripped from content
    assert out["content"] == [] and "_note" in out
    print("OK test_embedded_text_saved_and_stripped")


def test_embedded_blob_saved():
    p = McpExportArtifactPlugin()
    raw = bytes(range(20))
    out, ctx = _call(p, "mcp__db__export", _result([_embedded(blob=raw, mime="application/octet-stream")]))
    assert ctx.saved and out["_artifacts"][0]["bytes"] == len(raw)
    print("OK test_embedded_blob_saved")


def test_https_link_fetched(monkeypatch_httpx=None):
    p = McpExportArtifactPlugin()
    # Patch httpx.AsyncClient.get to return our bytes.
    import httpx
    payload = b"col1,col2\n10,20\n"

    class _Resp:
        content = payload
        def raise_for_status(self): pass

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, uri): return _Resp()

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        out, ctx = _call(p, "mcp__db__export",
                         _result([_link("https://example/export/orders.csv")]))
    finally:
        httpx.AsyncClient = orig
    assert ctx.saved and ctx.saved[0][0] == "orders.csv"
    assert out["_artifacts"][0]["bytes"] == len(payload)
    # link is NOT stripped (only embedded inline bytes are)
    assert any(c.get("type") == "resource_link" for c in out["content"])
    print("OK test_https_link_fetched")


def test_s3_link_referenced_no_copy():
    p = McpExportArtifactPlugin()
    out, ctx = _call(p, "mcp__db__export",
                     _result([_link("s3://adk-cc-test-bucket/exports/orders.parquet",
                                    name="orders.parquet")]))
    # s3 link recorded as a reference (no ctx.save_artifact call)
    assert not ctx.saved
    assert out is not None and out["_artifacts"], out
    print("OK test_s3_link_referenced_no_copy")


def test_audience_user_only_filters_assistant():
    p = McpExportArtifactPlugin()  # user_only default on
    out, ctx = _call(p, "mcp__db__export",
                     _result([_embedded(text="secret", audience=("assistant",))]))
    assert out is None and not ctx.saved
    print("OK test_audience_user_only_filters_assistant")


def test_audience_filter_off_saves_all():
    os.environ["ADK_CC_MCP_AUTOSAVE_AUDIENCE_USER_ONLY"] = "0"
    try:
        p = McpExportArtifactPlugin()
        out, ctx = _call(p, "mcp__db__export",
                         _result([_embedded(text="x", audience=("assistant",))]))
        assert ctx.saved and out is not None
    finally:
        os.environ["ADK_CC_MCP_AUTOSAVE_AUDIENCE_USER_ONLY"] = "1"
    print("OK test_audience_filter_off_saves_all")


def test_no_file_content_noops():
    p = McpExportArtifactPlugin()
    out, ctx = _call(p, "mcp__db__query",
                     _result([{"type": "text", "text": "[{\"a\":1}]"}],
                             structured={"rows": [{"a": 1}]}))
    assert out is None and not ctx.saved
    print("OK test_no_file_content_noops")


def test_malformed_result_noops():
    p = McpExportArtifactPlugin()
    out, ctx = _call(p, "mcp__db__query", {"not": "a call tool result"})
    assert out is None
    print("OK test_malformed_result_noops")


def test_file_scheme_link_skipped():
    p = McpExportArtifactPlugin()
    out, ctx = _call(p, "mcp__db__export",
                     _result([_link("file:///tmp/orders.csv")]))
    # file:// needs raw-URI session read (v1 skip) -> nothing saved -> None
    assert out is None and not ctx.saved
    print("OK test_file_scheme_link_skipped")


if __name__ == "__main__":
    test_disabled_gate_noops()
    test_non_mcp_tool_noops()
    test_embedded_text_saved_and_stripped()
    test_embedded_blob_saved()
    test_https_link_fetched()
    test_s3_link_referenced_no_copy()
    test_audience_user_only_filters_assistant()
    test_audience_filter_off_saves_all()
    test_no_file_content_noops()
    test_malformed_result_noops()
    test_file_scheme_link_skipped()
    print("\nall mcp-export-artifact-plugin tests passed")
