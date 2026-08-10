"""File upload delivery core (#121 P0).

An upload is a file at `uploads/<name>` in the session workspace, delivered
through the SAME backend write primitives the agent uses
(analysis/file-upload-plan.md). This suite pins:

  - name validation (single component, no traversal/dotfiles)
  - binary-exact delivery on the noop backend (+ atomicity artifacts absent)
  - ensure_workspace ordering (upload can precede the first turn)
  - overwrite policy (409-shaped error without, replace with)
  - the size cap (ADK_CC_UPLOAD_MAX_MB)
  - DockerBackend.write_bytes: binary-exact tar via put_archive, and the
    chunked base64 fallback on a read-only-rootfs APIError
  - write_text delegating to write_bytes (docker + sandbox_service), so
    the two paths cannot drift
  - the base fallback failing LOUDLY (naming the backend) on binary input

Run: ADK_CC_SKIP_DOTENV=1 PYTHONPATH=agents .venv/bin/python tests/test_upload_delivery.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shlex
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_SKIP_CONFIG_CHECK", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")
for _k in [k for k in os.environ if k.startswith("ADK_CC_UPLOAD")]:
    os.environ.pop(_k)

_passed = _failed = 0


def check(name, ok, detail=""):
    global _passed, _failed
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1


# Binary payload that is NOT valid utf-8 and big enough to cross chunk
# boundaries in the docker fallback (> 96 KiB of base64 input).
BLOB = bytes(range(256)) * 512  # 128 KiB


def _ws(root: str):
    from adk_cc.sandbox.workspace import WorkspaceRoot
    return WorkspaceRoot(tenant_id="local", session_id="s-up", abs_path=root)


def _noop():
    from adk_cc.sandbox.backends.noop_backend import NoopBackend
    return NoopBackend()


def main() -> int:
    from adk_cc.service import uploads as U

    # ---- name validation -------------------------------------------------
    # Real-world names must pass: browser-download suffixes, spaces,
    # non-Latin scripts, accents, plus/ampersand. The guards are structural
    # (separators, leading dot/dash, control chars, length), not a charset.
    ok_names = ["data.csv", "My Report 2.xlsx", "a-b_c.d.tar.gz", "x",
                "report (1).csv", "데이터 분석.xlsx", "売上データ.csv",
                "résumé.pdf", "data+v2.json", "P&L 2026.hwp", "회의록.docx"]
    for n in ok_names:
        try:
            check(f"name ok: {n!r}", U.check_upload_name(n) == n)
        except Exception as e:  # noqa: BLE001
            check(f"name ok: {n!r}", False, repr(e))
    bad_names = ["", "  ", "../x", "a/b", "a\\b", ".env", ".hidden",
                 "/etc/passwd", "a\x00b", "a\tb", "a\nb", "-flag.csv",
                 "x" * 200]
    for n in bad_names:
        try:
            U.check_upload_name(n)
            check(f"name rejected: {n!r}", False, "accepted")
        except U.UploadError:
            check(f"name rejected: {n!r}", True)

    # ---- delivery on the real noop backend -------------------------------
    with tempfile.TemporaryDirectory() as td:
        ws, be = _ws(td), _noop()
        row = asyncio.run(U.deliver_upload(ws, be, "data.bin", BLOB))
        dest = Path(td) / "uploads" / "data.bin"
        check("noop: file lands at uploads/<name>", dest.is_file())
        check("noop: bytes are binary-exact", dest.read_bytes() == BLOB)
        check("noop: no .part left behind",
              not list((Path(td) / "uploads").glob("*.part")))
        check("noop: row reports rel path + size",
              row["rel_path"] == "uploads/data.bin" and row["bytes"] == len(BLOB), row)

        # overwrite policy
        try:
            asyncio.run(U.deliver_upload(ws, be, "data.bin", b"x"))
            check("noop: second upload without overwrite raises 409", False)
        except U.UploadError as e:
            check("noop: second upload without overwrite raises 409",
                  e.status == 409, e.status)
        asyncio.run(U.deliver_upload(ws, be, "data.bin", b"x", overwrite=True))
        check("noop: overwrite replaces", dest.read_bytes() == b"x")

        # empty body
        try:
            asyncio.run(U.deliver_upload(ws, be, "e.bin", b""))
            check("empty body rejected", False)
        except U.UploadError as e:
            check("empty body rejected", e.status == 400, e.status)

        # images are just data: a real PNG header + payload survives intact
        png = (b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 64)
        asyncio.run(U.deliver_upload(ws, be, "chart.png", png))
        got = (Path(td) / "uploads" / "chart.png").read_bytes()
        check("image upload is byte-exact (PNG magic intact)",
              got == png and got[:8] == b"\x89PNG\r\n\x1a\n")

    # size cap
    os.environ["ADK_CC_UPLOAD_MAX_MB"] = "0.0001"  # ~104 bytes
    try:
        with tempfile.TemporaryDirectory() as td:
            try:
                asyncio.run(U.deliver_upload(_ws(td), _noop(), "big.bin", BLOB))
                check("size cap enforced (413)", False)
            except U.UploadError as e:
                check("size cap enforced (413)", e.status == 413, e.status)
    finally:
        os.environ.pop("ADK_CC_UPLOAD_MAX_MB", None)

    # ---- ensure_workspace runs BEFORE the write --------------------------
    calls: list[str] = []

    class _OrderProbe:
        name = "probe"

        async def ensure_workspace(self, ws):  # noqa: ANN001
            calls.append("ensure")

        async def write_bytes(self, path, content, *, fs_write):  # noqa: ANN001
            calls.append("write")

        async def exec(self, *a, **k):  # noqa: ANN002, ANN003
            class R: exit_code = 1
            return R()

    with tempfile.TemporaryDirectory() as td:
        asyncio.run(U.deliver_upload(_ws(td), _OrderProbe(), "f.bin", b"hi"))
    check("ensure_workspace precedes the write", calls == ["ensure", "write"], calls)

    # ---- DockerBackend.write_bytes ---------------------------------------
    import docker.errors as derr
    from adk_cc.sandbox.backends.docker_backend import DockerBackend

    class _FakeContainer:
        """Records put_archive / exec_run; optionally rejects put_archive
        the way a read-only rootfs does, and replays the fallback's shell
        commands against an in-memory file."""

        def __init__(self, read_only=False):
            self.read_only = read_only
            self.tar_bytes = None
            self.file = b""

        def exec_run(self, cmd, user=None, environment=None, workdir=None):  # noqa: ANN001
            script = cmd[-1] if isinstance(cmd, list) else str(cmd)
            if "mkdir -p" in script or (isinstance(cmd, list) and cmd[0] == "mkdir"):
                return 0, b""
            if "base64 -d" in script:
                # printf %s <quoted-b64> | base64 -d >(>)? <path>
                parts = shlex.split(script.split("|")[0])
                b64 = parts[-1]
                data = base64.b64decode(b64)
                if ">>" in script:
                    self.file += data
                else:
                    self.file = data
                return 0, b""
            if ": >" in script or ":>" in script:
                self.file = b""
                return 0, b""
            return 0, b""

        def put_archive(self, path, data):  # noqa: ANN001
            if self.read_only:
                raise derr.APIError(
                    "400 Client Error: container rootfs is marked read-only")
            self.tar_bytes = data
            return True

    def _docker_with(container):
        be = DockerBackend.__new__(DockerBackend)  # skip client init
        be._workspace_abs_path = "/srv/ws"

        async def _ensure():
            return container
        be._ensure_container = _ensure
        return be

    class _AllowAll:
        def allows(self, path):  # noqa: ANN001
            return True

    # happy path: tar carries the exact bytes
    c = _FakeContainer()
    asyncio.run(_docker_with(c).write_bytes(
        "/srv/ws/uploads/b.bin", BLOB, fs_write=_AllowAll()))
    check("docker: put_archive received a tar", c.tar_bytes is not None)
    if c.tar_bytes:
        with tarfile.open(fileobj=io.BytesIO(c.tar_bytes)) as tf:
            member = tf.getmembers()[0]
            got = tf.extractfile(member).read()
        check("docker: tar member is binary-exact", got == BLOB)
        check("docker: tar member named after the file", member.name == "b.bin")

    # read-only fallback: chunked base64 reconstructs the exact bytes
    c2 = _FakeContainer(read_only=True)
    asyncio.run(_docker_with(c2).write_bytes(
        "/srv/ws/uploads/b.bin", BLOB, fs_write=_AllowAll()))
    check("docker: read-only fallback reconstructs binary-exact bytes",
          c2.file == BLOB, f"got {len(c2.file)} bytes, want {len(BLOB)}")

    # write_text delegates to write_bytes (no drift)
    seen: dict = {}

    async def _spy(path, content, *, fs_write):  # noqa: ANN001
        seen["data"] = content
    be = _docker_with(_FakeContainer())
    be.write_bytes = _spy
    asyncio.run(be.write_text("/srv/ws/x.txt", "héllo", fs_write=_AllowAll()))
    check("docker: write_text delegates to write_bytes",
          seen.get("data") == "héllo".encode("utf-8"))

    from adk_cc.sandbox.backends.sandbox_service_backend import (
        SandboxServiceBackend,
    )
    seen2: dict = {}
    sb = SandboxServiceBackend.__new__(SandboxServiceBackend)

    async def _spy2(path, content, *, fs_write):  # noqa: ANN001
        seen2["data"] = content
    sb.write_bytes = _spy2
    asyncio.run(sb.write_text("/ws/x.txt", "héllo", fs_write=_AllowAll()))
    check("sandbox_service: write_text delegates to write_bytes",
          seen2.get("data") == "héllo".encode("utf-8"))

    # ---- base fallback is LOUD on binary ---------------------------------
    from adk_cc.sandbox.backends.base import SandboxBackend

    class _TextOnly(SandboxBackend):
        name = "textonly"

        async def exec(self, *a, **k):  # noqa: ANN002, ANN003
            raise NotImplementedError

        async def exec_stream(self, *a, **k):  # noqa: ANN002, ANN003
            raise NotImplementedError

        async def read_text(self, path, *, fs_read):  # noqa: ANN001
            raise NotImplementedError

        async def write_text(self, path, content, *, fs_write):  # noqa: ANN001
            pass

    try:
        asyncio.run(_TextOnly().write_bytes("/x", b"\xff\xfe", fs_write=_AllowAll()))
        check("base write_bytes: binary on a text-only backend fails loudly", False,
              "no error raised")
    except Exception as e:  # noqa: BLE001
        check("base write_bytes: binary on a text-only backend fails loudly",
              "textonly" in str(e) and "binary" in str(e).lower(), repr(e))

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
