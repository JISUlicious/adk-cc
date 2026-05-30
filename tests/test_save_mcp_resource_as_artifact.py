"""Tests for SaveMcpResourceAsArtifactTool (Pattern A).

Fakes a bound McpToolset (`read_resource`) + a ToolContext (`save_artifact`
+ `_invocation_context.artifact_service`) and drives the tool's run_async.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import base64
import os
import types as pytypes

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from adk_cc.tools.save_mcp_resource_as_artifact import (
    SaveMcpResourceAsArtifactTool,
    _safe_name,
)


# --- fakes ----------------------------------------------------------------

def _text(text, mime="text/plain"):
    return pytypes.SimpleNamespace(text=text, blob=None, mimeType=mime)


def _blob(raw: bytes, mime="application/octet-stream"):
    return pytypes.SimpleNamespace(text=None, blob=base64.b64encode(raw).decode(), mimeType=mime)


class _FakeToolset:
    def __init__(self, contents=None, *, raises=None):
        self._contents = contents or []
        self._raises = raises
        self.read_calls = []

    async def read_resource(self, name):
        self.read_calls.append(name)
        if self._raises is not None:
            raise self._raises
        return self._contents


class _FakeArtifactService:
    def __init__(self):
        self.calls = []

    async def save_artifact(self, *, app_name, user_id, session_id, filename, artifact):
        self.calls.append(dict(session_id=session_id, filename=filename))
        return 99


class _FakeCtx:
    def __init__(self, artifact_service=None):
        if artifact_service is None:
            artifact_service = _FakeArtifactService()
        self._invocation_context = pytypes.SimpleNamespace(
            artifact_service=artifact_service, app_name="adk_cc", user_id="alice"
        )
        self.session_saves = []

    async def save_artifact(self, filename, part):
        self.session_saves.append((filename, part))
        return len(self.session_saves) - 1


def _tool(toolset):
    return SaveMcpResourceAsArtifactTool(mcp_toolset=toolset, server_name="db")


def _run(coro):
    return asyncio.run(coro)


# --- tests ----------------------------------------------------------------

def test_safe_name():
    assert _safe_name("db://schema/users") == "schema_users"
    assert _safe_name("report.csv") == "report.csv"
    assert _safe_name("file:///a/b/c.txt") == "a_b_c.txt"
    assert _safe_name("///") == "resource"
    print("OK test_safe_name")


def test_text_resource_session():
    ts = _FakeToolset([_text("hello", "text/markdown")])
    ctx = _FakeCtx()
    out = _run(_tool(ts).run_async(args={"resource_name": "notes"}, tool_context=ctx))
    assert out["status"] == "ok" and out["scope"] == "session"
    assert out["filename"] == "notes" and out["bytes"] == 5 and out["mime_type"] == "text/markdown"
    assert ctx.session_saves[0][0] == "notes"
    print("OK test_text_resource_session")


def test_blob_resource_session():
    raw = bytes([0x89, 0x50, 0x4E, 0x47, 1, 2, 3])
    ts = _FakeToolset([_blob(raw, "image/png")])
    ctx = _FakeCtx()
    out = _run(_tool(ts).run_async(args={"resource_name": "img", "filename": "pic.png"}, tool_context=ctx))
    assert out["status"] == "ok" and out["filename"] == "pic.png"
    assert out["bytes"] == len(raw) and out["mime_type"] == "image/png"
    print("OK test_blob_resource_session")


def test_user_scope_uses_service():
    svc = _FakeArtifactService()
    ts = _FakeToolset([_text("persist me")])
    ctx = _FakeCtx(artifact_service=svc)
    out = _run(_tool(ts).run_async(args={"resource_name": "keep", "scope": "user"}, tool_context=ctx))
    assert out["status"] == "ok" and out["scope"] == "user" and out["version"] == 99
    assert not ctx.session_saves and svc.calls[0]["session_id"] is None
    print("OK test_user_scope_uses_service")


def test_filename_default_from_resource_name():
    ts = _FakeToolset([_text("x")])
    ctx = _FakeCtx()
    out = _run(_tool(ts).run_async(args={"resource_name": "db://schema/orders"}, tool_context=ctx))
    assert out["filename"] == "schema_orders"
    print("OK test_filename_default_from_resource_name")


def test_multi_content_indexed():
    ts = _FakeToolset([_text("part0"), _blob(b"part1bytes")])
    ctx = _FakeCtx()
    out = _run(_tool(ts).run_async(args={"resource_name": "dir", "filename": "bundle"}, tool_context=ctx))
    assert out["status"] == "ok" and out["count"] == 2
    names = [s["filename"] for s in out["saved"]]
    assert names == ["bundle.0", "bundle.1"], names
    print("OK test_multi_content_indexed")


def test_empty_contents_errors():
    ts = _FakeToolset([])
    out = _run(_tool(ts).run_async(args={"resource_name": "void"}, tool_context=_FakeCtx()))
    assert out["status"] == "error" and "no contents" in out["error"]
    print("OK test_empty_contents_errors")


def test_resource_not_found():
    ts = _FakeToolset(raises=ValueError("Resource with name 'nope' not found."))
    out = _run(_tool(ts).run_async(args={"resource_name": "nope"}, tool_context=_FakeCtx()))
    assert out["status"] == "not_found" and "not found" in out["error"]
    print("OK test_resource_not_found")


def test_read_connection_error():
    ts = _FakeToolset(raises=ConnectionError("server down"))
    out = _run(_tool(ts).run_async(args={"resource_name": "x"}, tool_context=_FakeCtx()))
    assert out["status"] == "error" and "read_resource" in out["error"]
    print("OK test_read_connection_error")


def test_unsupported_content():
    bad = pytypes.SimpleNamespace(text=None, blob=None, mimeType="x")
    ts = _FakeToolset([bad])
    out = _run(_tool(ts).run_async(args={"resource_name": "x"}, tool_context=_FakeCtx()))
    assert out["status"] == "error" and "unsupported content" in out["error"]
    print("OK test_unsupported_content")


def test_invalid_scope():
    ts = _FakeToolset([_text("x")])
    out = _run(_tool(ts).run_async(args={"resource_name": "x", "scope": "bogus"}, tool_context=_FakeCtx()))
    assert out["status"] == "error" and "scope" in out["error"]
    print("OK test_invalid_scope")


def test_input_validation_error():
    ts = _FakeToolset([_text("x")])
    out = _run(_tool(ts).run_async(args={}, tool_context=_FakeCtx()))  # missing resource_name
    assert out["status"] == "input_validation_error"
    print("OK test_input_validation_error")


def test_missing_artifact_service():
    ts = _FakeToolset([_text("x")])
    ctx = _FakeCtx()
    ctx._invocation_context.artifact_service = None
    out = _run(_tool(ts).run_async(args={"resource_name": "x"}, tool_context=ctx))
    assert out["status"] == "error" and "ADK_CC_ARTIFACT_STORAGE_URI" in out["error"]
    print("OK test_missing_artifact_service")


def test_declaration_shape():
    decl = _tool(_FakeToolset()) ._get_declaration()
    assert decl.name == "save_resource_as_artifact"
    print("OK test_declaration_shape")


if __name__ == "__main__":
    test_safe_name()
    test_text_resource_session()
    test_blob_resource_session()
    test_user_scope_uses_service()
    test_filename_default_from_resource_name()
    test_multi_content_indexed()
    test_empty_contents_errors()
    test_resource_not_found()
    test_read_connection_error()
    test_unsupported_content()
    test_invalid_scope()
    test_input_validation_error()
    test_missing_artifact_service()
    test_declaration_shape()
    print("\nall save-mcp-resource tests passed")
