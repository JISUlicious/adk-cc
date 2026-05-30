"""Tests for S3ArtifactService.

Exercise the full storage logic against an in-memory fake S3 client (no
real bucket / no boto3 network calls), plus the s3:// scheme registration.

Hand-rolled (no pytest), runnable with the venv python.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

os.environ.setdefault("ADK_CC_SKIP_DOTENV", "1")
os.environ.setdefault("ADK_CC_API_KEY", "stub")

from google.genai import types

from adk_cc.artifacts import S3ArtifactService, register_s3_artifact_scheme

_DT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# --- fake S3 client -------------------------------------------------------

class _ClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _Paginator:
    def __init__(self, store: dict) -> None:
        self._store = store

    def paginate(self, *, Bucket, Prefix):  # noqa: N803 (boto3 casing)
        contents = [
            {"Key": k, "LastModified": v["LastModified"]}
            for k, v in sorted(self._store.items())
            if k.startswith(Prefix)
        ]
        # Emulate two pages to exercise pagination handling.
        mid = len(contents) // 2
        if mid:
            yield {"Contents": contents[:mid]}
            yield {"Contents": contents[mid:]}
        else:
            yield {"Contents": contents}


class _FakeS3:
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    def put_object(self, *, Bucket, Key, Body, ContentType=None, Metadata=None):  # noqa: N803
        self.store[Key] = {
            "Body": Body,
            "ContentType": ContentType,
            "Metadata": Metadata or {},
            "LastModified": _DT,
        }
        return {}

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise _ClientError("NoSuchKey", 404)
        o = self.store[Key]
        return {"Body": _Body(o["Body"]), "ContentType": o["ContentType"]}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.store:
            raise _ClientError("404", 404)
        o = self.store[Key]
        return {
            "ContentType": o["ContentType"],
            "Metadata": o["Metadata"],
            "LastModified": o["LastModified"],
        }

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.store.pop(Key, None)
        return {}

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator(self.store)


def _svc(prefix: str = "") -> tuple[S3ArtifactService, _FakeS3]:
    fake = _FakeS3()
    return S3ArtifactService(bucket_name="bkt", prefix=prefix, client=fake), fake


def _part(text: str) -> types.Part:
    return types.Part.from_bytes(data=text.encode(), mime_type="text/plain")


def _run(coro):
    return asyncio.run(coro)


# --- tests ----------------------------------------------------------------

def test_save_increments_versions_and_loads():
    svc, _ = _svc()
    v0 = _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f.txt", artifact=_part("hello")))
    v1 = _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f.txt", artifact=_part("world")))
    assert (v0, v1) == (0, 1), (v0, v1)

    latest = _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="f.txt"))
    assert latest.inline_data.data == b"world"
    first = _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="f.txt", version=0))
    assert first.inline_data.data == b"hello"
    print("OK test_save_increments_versions_and_loads")


def test_list_versions_and_keys():
    svc, _ = _svc()
    for t in ("a", "b", "c"):
        _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="data.bin", artifact=_part(t)))
    versions = _run(svc.list_versions(app_name="a", user_id="u", session_id="s", filename="data.bin"))
    assert sorted(versions) == [0, 1, 2], versions
    keys = _run(svc.list_artifact_keys(app_name="a", user_id="u", session_id="s"))
    assert keys == ["data.bin"], keys
    print("OK test_list_versions_and_keys")


def test_user_namespace_scope():
    svc, fake = _svc()
    # user-scoped (session_id None, "user:" prefix)
    _run(svc.save_artifact(app_name="a", user_id="u", session_id=None, filename="user:profile", artifact=_part("p")))
    # session-scoped
    _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="sess.txt", artifact=_part("x")))
    # keys land in the documented layout
    assert "a/u/user/user:profile/0" in fake.store
    assert "a/u/s/sess.txt/0" in fake.store
    # listing with a session returns BOTH session- and user-scoped names
    keys = _run(svc.list_artifact_keys(app_name="a", user_id="u", session_id="s"))
    assert set(keys) == {"sess.txt", "user:profile"}, keys
    # listing with no session returns only user-scoped
    user_only = _run(svc.list_artifact_keys(app_name="a", user_id="u", session_id=None))
    assert user_only == ["user:profile"], user_only
    print("OK test_user_namespace_scope")


def test_delete_removes_all_versions():
    svc, _ = _svc()
    for t in ("1", "2"):
        _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f", artifact=_part(t)))
    _run(svc.delete_artifact(app_name="a", user_id="u", session_id="s", filename="f"))
    assert _run(svc.list_versions(app_name="a", user_id="u", session_id="s", filename="f")) == []
    assert _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="f")) is None
    print("OK test_delete_removes_all_versions")


def test_version_metadata():
    svc, _ = _svc()
    _run(svc.save_artifact(
        app_name="a", user_id="u", session_id="s", filename="f.txt",
        artifact=_part("x"), custom_metadata={"author": "alice", "n": 3},
    ))
    _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f.txt", artifact=_part("y")))

    versions = _run(svc.list_artifact_versions(app_name="a", user_id="u", session_id="s", filename="f.txt"))
    assert [v.version for v in versions] == [0, 1], versions
    assert versions[0].canonical_uri == "s3://bkt/a/u/s/f.txt/0"
    assert versions[0].mime_type == "text/plain"
    assert versions[0].custom_metadata == {"author": "alice", "n": "3"}  # stringified
    assert versions[0].create_time == _DT.timestamp()

    latest = _run(svc.get_artifact_version(app_name="a", user_id="u", session_id="s", filename="f.txt"))
    assert latest.version == 1
    v0 = _run(svc.get_artifact_version(app_name="a", user_id="u", session_id="s", filename="f.txt", version=0))
    assert v0.version == 0
    print("OK test_version_metadata")


def test_missing_returns_none():
    svc, _ = _svc()
    _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f", artifact=_part("x")))
    # a version that doesn't exist -> get_object 404 -> None
    assert _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="f", version=99)) is None
    # unknown filename -> no versions -> None
    assert _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="nope")) is None
    assert _run(svc.get_artifact_version(app_name="a", user_id="u", session_id="s", filename="nope")) is None
    print("OK test_missing_returns_none")


def test_bucket_prefix_applied():
    svc, fake = _svc(prefix="arts")
    _run(svc.save_artifact(app_name="a", user_id="u", session_id="s", filename="f", artifact=_part("x")))
    assert "arts/a/u/s/f/0" in fake.store, list(fake.store)
    # round-trips through the prefix
    assert _run(svc.load_artifact(app_name="a", user_id="u", session_id="s", filename="f")).inline_data.data == b"x"
    print("OK test_bucket_prefix_applied")


def test_scheme_registration():
    register_s3_artifact_scheme()
    from google.adk.cli.service_registry import get_service_registry

    svc = get_service_registry().create_artifact_service("s3://my-bucket/some/prefix", agents_dir=".")
    assert isinstance(svc, S3ArtifactService)
    assert svc.bucket_name == "my-bucket"
    assert svc._prefix == "some/prefix/"
    print("OK test_scheme_registration")


if __name__ == "__main__":
    test_save_increments_versions_and_loads()
    test_list_versions_and_keys()
    test_user_namespace_scope()
    test_delete_removes_all_versions()
    test_version_metadata()
    test_missing_returns_none()
    test_bucket_prefix_applied()
    test_scheme_registration()
    print("\nall s3-artifact tests passed")
