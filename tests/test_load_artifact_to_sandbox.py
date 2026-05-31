"""Tests for LoadArtifactToSandboxTool (artifact → sandbox copy).

Seeds a fake SandboxBackend + WorkspaceRoot into ctx.state (so
get_backend/get_workspace/resolve work) and a fake artifact load on the
ctx, then drives _execute.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import os
import types as pytypes

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.sandbox.backends.base import SandboxBackend
from adk_cc.sandbox.config import FsReadConfig, FsWriteConfig, SandboxViolation
from adk_cc.sandbox.workspace import WorkspaceRoot
from adk_cc.tools.load_artifact_to_sandbox import LoadArtifactToSandboxTool
from adk_cc.tools.schemas import LoadArtifactToSandboxArgs

_WS_ROOT = "/work/acme/alice"


# --- fakes ----------------------------------------------------------------

class _FakeBackend(SandboxBackend):
    """Records write_bytes; serves read_bytes from an in-memory store."""

    def __init__(self, *, existing: dict[str, bytes] | None = None,
                 write_raises: Exception | None = None):
        self.files: dict[str, bytes] = dict(existing or {})
        self.writes: list[tuple[str, bytes]] = []
        self._write_raises = write_raises

    async def read_bytes(self, path, *, fs_read):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_bytes(self, path, content, *, fs_write):
        if self._write_raises is not None:
            raise self._write_raises
        self.writes.append((path, content))
        self.files[path] = content

    # unused ABC methods
    async def read_text(self, path, *, fs_read): raise NotImplementedError
    async def write_text(self, path, content, *, fs_write): raise NotImplementedError
    async def exec(self, *a, **k): raise NotImplementedError


class _State(dict):
    pass


class _Ctx:
    def __init__(self, backend, *, load_result="__sentinel__", load_raises=None,
                 artifact_service=None):
        ws = WorkspaceRoot(tenant_id="acme", session_id="s1", abs_path=_WS_ROOT)
        self.state = _State({
            "temp:sandbox_workspace": ws,
            "temp:sandbox_backend": backend,
        })
        self._load_result = load_result
        self._load_raises = load_raises
        self.load_calls = []
        self._invocation_context = pytypes.SimpleNamespace(
            artifact_service=artifact_service, app_name="adk_cc", user_id="alice"
        )

    async def load_artifact(self, filename, version=None):
        self.load_calls.append((filename, version))
        if self._load_raises is not None:
            raise self._load_raises
        return self._load_result


def _part(data: bytes, mime="text/plain") -> types.Part:
    return types.Part(inline_data=types.Blob(data=data, mime_type=mime))


def _run(coro):
    return asyncio.run(coro)


def _call(ctx, **kw):
    args = LoadArtifactToSandboxArgs(**kw)
    return _run(LoadArtifactToSandboxTool()._execute(args, ctx))


# --- tests ----------------------------------------------------------------

def test_loads_latest_into_sandbox():
    be = _FakeBackend()
    ctx = _Ctx(be, load_result=_part(b"hello world", "text/plain"))
    out = _call(ctx, filename="notes.txt", dest_path="out/notes.txt")
    assert out["status"] == "ok", out
    assert out["bytes"] == 11 and out["mime_type"] == "text/plain"
    assert out["dest_path"] == "out/notes.txt"  # workspace-relative echo
    assert ctx.load_calls == [("notes.txt", None)]  # latest
    # written into the sandbox at the resolved abs path
    assert (f"{_WS_ROOT}/out/notes.txt", b"hello world") in be.writes
    print("OK test_loads_latest_into_sandbox")


def test_pins_version():
    be = _FakeBackend()
    ctx = _Ctx(be, load_result=_part(b"v2 bytes"))
    out = _call(ctx, filename="data.bin", dest_path="data.bin", version=2)
    assert out["status"] == "ok" and out["version"] == 2
    assert ctx.load_calls == [("data.bin", 2)]
    print("OK test_pins_version")


def test_binary_roundtrip():
    raw = bytes([0x89, 0x50, 0x4E, 0x47, 0, 255, 16, 0])
    be = _FakeBackend()
    ctx = _Ctx(be, load_result=_part(raw, "image/png"))
    out = _call(ctx, filename="img.png", dest_path="img.png")
    assert out["status"] == "ok" and be.files[f"{_WS_ROOT}/img.png"] == raw
    print("OK test_binary_roundtrip")


def test_not_found_when_artifact_missing():
    be = _FakeBackend()
    ctx = _Ctx(be, load_result=None)  # ADK returns None when absent
    out = _call(ctx, filename="nope.txt", dest_path="nope.txt")
    assert out["status"] == "not_found" and not be.writes
    print("OK test_not_found_when_artifact_missing")


def test_clobber_guard_blocks_existing():
    dest_abs = f"{_WS_ROOT}/exists.txt"
    be = _FakeBackend(existing={dest_abs: b"old sandbox content"})
    ctx = _Ctx(be, load_result=_part(b"new"))
    out = _call(ctx, filename="exists.txt", dest_path="exists.txt")
    assert out["status"] == "exists" and not be.writes  # nothing written
    assert be.files[dest_abs] == b"old sandbox content"  # untouched
    print("OK test_clobber_guard_blocks_existing")


def test_overwrite_true_replaces():
    dest_abs = f"{_WS_ROOT}/exists.txt"
    be = _FakeBackend(existing={dest_abs: b"old"})
    ctx = _Ctx(be, load_result=_part(b"new content"))
    out = _call(ctx, filename="exists.txt", dest_path="exists.txt", overwrite=True)
    assert out["status"] == "ok" and be.files[dest_abs] == b"new content"
    print("OK test_overwrite_true_replaces")


def test_user_scope_uses_artifact_service():
    be = _FakeBackend()

    class _Svc:
        def __init__(self): self.calls = []
        async def load_artifact(self, *, app_name, user_id, session_id, filename, version):
            self.calls.append(dict(session_id=session_id, filename=filename, version=version))
            return _part(b"user-scoped")

    svc = _Svc()
    ctx = _Ctx(be, artifact_service=svc)
    out = _call(ctx, filename="keep.txt", dest_path="keep.txt", scope="user")
    assert out["status"] == "ok" and out["scope"] == "user"
    assert ctx.load_calls == []  # bypassed ctx.load_artifact
    assert svc.calls[0]["session_id"] is None
    print("OK test_user_scope_uses_artifact_service")


def test_invalid_scope():
    ctx = _Ctx(_FakeBackend(), load_result=_part(b"x"))
    out = _call(ctx, filename="x", dest_path="x", scope="bogus")
    assert out["status"] == "error" and "scope" in out["error"]
    print("OK test_invalid_scope")


def test_sandbox_violation_surfaced():
    be = _FakeBackend(write_raises=SandboxViolation("path not allowed"))
    ctx = _Ctx(be, load_result=_part(b"x"))
    out = _call(ctx, filename="x", dest_path="/etc/passwd")
    assert out["status"] == "sandbox_denied"
    print("OK test_sandbox_violation_surfaced")


def test_user_scope_missing_service():
    ctx = _Ctx(_FakeBackend(), artifact_service=None)
    out = _call(ctx, filename="x", dest_path="x", scope="user")
    assert out["status"] == "error" and "ADK_CC_ARTIFACT_STORAGE_URI" in out["error"]
    print("OK test_user_scope_missing_service")


if __name__ == "__main__":
    test_loads_latest_into_sandbox()
    test_pins_version()
    test_binary_roundtrip()
    test_not_found_when_artifact_missing()
    test_clobber_guard_blocks_existing()
    test_overwrite_true_replaces()
    test_user_scope_uses_artifact_service()
    test_invalid_scope()
    test_sandbox_violation_surfaced()
    test_user_scope_missing_service()
    print("\nall load-artifact-to-sandbox tests passed")
