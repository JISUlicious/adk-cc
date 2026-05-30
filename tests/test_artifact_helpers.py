"""Tests for the shared artifact helpers.

  - tools/_artifact.py::save_part_as_artifact (scope branch, service
    lookup, result dict, error paths)
  - tools/_mcp_content.py::mcp_content_to_part (text/blob/embedded/unsupported)

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import base64
import os
import types as pytypes

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.tools._artifact import save_part_as_artifact
from adk_cc.tools._mcp_content import mcp_content_to_part


# --- fakes ----------------------------------------------------------------

class _FakeArtifactService:
    def __init__(self):
        self.calls = []

    async def save_artifact(self, *, app_name, user_id, session_id, filename, artifact):
        self.calls.append(
            dict(app_name=app_name, user_id=user_id, session_id=session_id,
                 filename=filename, artifact=artifact)
        )
        return 7  # arbitrary user-scope version


class _FakeInvocationContext:
    def __init__(self, artifact_service):
        self.artifact_service = artifact_service
        self.app_name = "adk_cc"
        self.user_id = "alice"


class _FakeCtx:
    """Mimics ToolContext enough for the helper."""
    def __init__(self, *, artifact_service):
        self._invocation_context = (
            _FakeInvocationContext(artifact_service)
            if artifact_service is not None
            else pytypes.SimpleNamespace(artifact_service=None)
        )
        self.session_saves = []

    async def save_artifact(self, filename, part):
        self.session_saves.append((filename, part))
        return len(self.session_saves) - 1  # 0-based session version


def _part(data: bytes, mime: str) -> types.Part:
    return types.Part(inline_data=types.Blob(data=data, mime_type=mime))


def _run(coro):
    return asyncio.run(coro)


# --- mcp_content_to_part --------------------------------------------------

def test_content_text():
    c = pytypes.SimpleNamespace(text="hello", blob=None, mimeType="text/markdown")
    p = mcp_content_to_part(c)
    assert p.inline_data.data == b"hello"
    assert p.inline_data.mime_type == "text/markdown"
    print("OK test_content_text")


def test_content_text_default_mime():
    c = pytypes.SimpleNamespace(text="x", blob=None, mimeType=None)
    assert mcp_content_to_part(c).inline_data.mime_type == "text/plain"
    print("OK test_content_text_default_mime")


def test_content_blob():
    raw = bytes([0x89, 0x50, 0x4E, 0x47, 0, 255, 16])
    c = pytypes.SimpleNamespace(text=None, blob=base64.b64encode(raw).decode(), mimeType="image/png")
    p = mcp_content_to_part(c)
    assert p.inline_data.data == raw and p.inline_data.mime_type == "image/png"
    print("OK test_content_blob")


def test_content_embedded_resource_unwrap():
    # EmbeddedResource: payload nested under .resource
    inner = pytypes.SimpleNamespace(text="nested", blob=None, mimeType="text/csv")
    emb = pytypes.SimpleNamespace(type="resource", resource=inner)
    p = mcp_content_to_part(emb)
    assert p.inline_data.data == b"nested" and p.inline_data.mime_type == "text/csv"
    print("OK test_content_embedded_resource_unwrap")


def test_content_unsupported_returns_none():
    c = pytypes.SimpleNamespace(text=None, blob=None, mimeType="x")
    assert mcp_content_to_part(c) is None
    assert mcp_content_to_part(None) is None
    print("OK test_content_unsupported_returns_none")


def test_content_bad_base64_returns_none():
    c = pytypes.SimpleNamespace(text=None, blob="!!!not base64!!!", mimeType="x")
    # b64decode is lenient; force a clearly invalid value with validate semantics
    c2 = pytypes.SimpleNamespace(text=None, blob="A", mimeType="x")  # len%4 != 0 -> error
    assert mcp_content_to_part(c2) is None
    print("OK test_content_bad_base64_returns_none")


# --- save_part_as_artifact ------------------------------------------------

def test_save_session_scope():
    ctx = _FakeCtx(artifact_service=_FakeArtifactService())
    out = _run(save_part_as_artifact(ctx, filename="r.txt", part=_part(b"abc", "text/plain"), scope="session"))
    assert out == {"status": "ok", "filename": "r.txt", "version": 0,
                   "scope": "session", "bytes": 3, "mime_type": "text/plain"}, out
    assert ctx.session_saves and ctx.session_saves[0][0] == "r.txt"
    print("OK test_save_session_scope")


def test_save_user_scope_uses_service():
    svc = _FakeArtifactService()
    ctx = _FakeCtx(artifact_service=svc)
    out = _run(save_part_as_artifact(ctx, filename="keep.bin", part=_part(b"0123", "application/octet-stream"), scope="user"))
    assert out["status"] == "ok" and out["scope"] == "user" and out["version"] == 7 and out["bytes"] == 4
    assert not ctx.session_saves  # bypassed ctx.save_artifact
    assert svc.calls[0]["session_id"] is None and svc.calls[0]["filename"] == "keep.bin"
    print("OK test_save_user_scope_uses_service")


def test_save_default_scope_is_session():
    ctx = _FakeCtx(artifact_service=_FakeArtifactService())
    out = _run(save_part_as_artifact(ctx, filename="d.txt", part=_part(b"z", "text/plain"), scope=""))
    assert out["scope"] == "session"
    print("OK test_save_default_scope_is_session")


def test_save_invalid_scope():
    ctx = _FakeCtx(artifact_service=_FakeArtifactService())
    out = _run(save_part_as_artifact(ctx, filename="x", part=_part(b"z", "text/plain"), scope="bogus"))
    assert out["status"] == "error" and "scope" in out["error"]
    print("OK test_save_invalid_scope")


def test_save_missing_artifact_service():
    ctx = _FakeCtx(artifact_service=None)
    out = _run(save_part_as_artifact(ctx, filename="x", part=_part(b"z", "text/plain"), scope="session"))
    assert out["status"] == "error" and "ADK_CC_ARTIFACT_STORAGE_URI" in out["error"]
    print("OK test_save_missing_artifact_service")


def test_save_no_inline_data():
    ctx = _FakeCtx(artifact_service=_FakeArtifactService())
    out = _run(save_part_as_artifact(ctx, filename="x", part=types.Part(text="not inline"), scope="session"))
    assert out["status"] == "error" and "inline_data" in out["error"]
    print("OK test_save_no_inline_data")


if __name__ == "__main__":
    test_content_text()
    test_content_text_default_mime()
    test_content_blob()
    test_content_embedded_resource_unwrap()
    test_content_unsupported_returns_none()
    test_content_bad_base64_returns_none()
    test_save_session_scope()
    test_save_user_scope_uses_service()
    test_save_default_scope_is_session()
    test_save_invalid_scope()
    test_save_missing_artifact_service()
    test_save_no_inline_data()
    print("\nall artifact-helper tests passed")
