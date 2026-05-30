"""An ADK artifact service backed by S3 or any S3-compatible object store.

ADK 1.31 ships InMemory / File / GCS artifact services but no S3, and the
two community packages don't fit:
  - `google-adk-aws` has no custom `endpoint_url`, so it's AWS-only (no
    MinIO / R2 / Wasabi).
  - `adk-extra-services` (v0.1.7) implements only 5 of the 7 abstract
    methods — it omits `list_artifact_versions` / `get_artifact_version`,
    which are `@abstractmethod` in 1.31.1, so the class can't even be
    instantiated here — and it calls blocking boto3 directly inside
    `async def`, stalling the event loop.

This implementation mirrors ADK's own `GcsArtifactService` for
correctness — every blocking boto3 call runs in `asyncio.to_thread`, and
all seven `BaseArtifactService` methods are implemented — while taking
the `endpoint_url` + explicit-credentials shape that makes S3-compatible
stores work.

Object-key layout (identical to GcsArtifactService, plus an optional
bucket prefix):
  - user-scoped (filename starts with "user:"):
        {prefix}{app_name}/{user_id}/user/{filename}/{version}
  - session-scoped:
        {prefix}{app_name}/{user_id}/{session_id}/{filename}/{version}

Versions are 0-based and monotonically increasing, computed by listing
existing objects under the artifact's key prefix (same as GCS).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Union
from urllib.parse import urlparse

from google.adk.artifacts.base_artifact_service import (
    ArtifactVersion,
    BaseArtifactService,
    ensure_part,
)
from google.genai import types

logger = logging.getLogger("adk_cc." + __name__)


class S3ArtifactService(BaseArtifactService):
    """Artifact service for AWS S3 / S3-compatible object storage.

    Args:
        bucket_name: Target bucket. Must already exist.
        prefix: Optional key prefix within the bucket (e.g. "artifacts").
            A trailing slash is added if missing; "" means bucket root.
        endpoint_url: Custom endpoint for S3-compatible stores
            (MinIO/R2/Wasabi/Ceph/Spaces). Omit for AWS S3.
        aws_access_key_id / aws_secret_access_key: Static credentials.
            When omitted, boto3's default chain is used (env vars, shared
            config, instance/role profile, etc.).
        region_name: AWS region (or compatible-store region).
        client: A pre-built boto3 S3 client. Takes precedence over the
            connection kwargs above — used by tests to inject a fake.
        **client_kwargs: Extra kwargs forwarded to ``boto3.client('s3')``.
    """

    def __init__(
        self,
        bucket_name: str,
        *,
        prefix: str = "",
        endpoint_url: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        region_name: Optional[str] = None,
        client: Any = None,
        **client_kwargs: Any,
    ) -> None:
        self.bucket_name = bucket_name
        self._prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""

        if client is not None:
            self.s3_client = client
        else:
            # Lazy import so the module loads even when boto3 isn't
            # installed — it's an optional extra, only needed when an
            # s3:// artifact URI is actually configured.
            import boto3

            if endpoint_url:
                client_kwargs["endpoint_url"] = endpoint_url
            if aws_access_key_id:
                client_kwargs["aws_access_key_id"] = aws_access_key_id
            if aws_secret_access_key:
                client_kwargs["aws_secret_access_key"] = aws_secret_access_key
            if region_name:
                client_kwargs["region_name"] = region_name
            self.s3_client = boto3.client("s3", **client_kwargs)

    # --- key construction -------------------------------------------------

    def _file_has_user_namespace(self, filename: str) -> bool:
        return filename.startswith("user:")

    def _get_key_prefix(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str],
    ) -> str:
        """Key prefix (without the trailing /version) for an artifact."""
        if self._file_has_user_namespace(filename):
            return f"{self._prefix}{app_name}/{user_id}/user/{filename}"
        if session_id is None:
            raise ValueError(
                "session_id must be provided for session-scoped artifacts."
            )
        return f"{self._prefix}{app_name}/{user_id}/{session_id}/{filename}"

    def _get_key(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        version: int,
        session_id: Optional[str],
    ) -> str:
        return (
            f"{self._get_key_prefix(app_name, user_id, filename, session_id)}"
            f"/{version}"
        )

    # --- low-level S3 helpers (sync; always called via to_thread) ---------

    def _list_keys(self, prefix: str) -> list[str]:
        """All object keys under `prefix`, paginated."""
        keys: list[str] = []
        paginator = self.s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                keys.append(obj["Key"])
        return keys

    def _versions_under(self, prefix: str) -> list[int]:
        versions: list[int] = []
        for key in self._list_keys(f"{prefix}/"):
            tail = key.rsplit("/", 1)[-1]
            try:
                versions.append(int(tail))
            except ValueError:
                logger.warning(
                    "Skipping object %s: tail %r is not a version number.",
                    key,
                    tail,
                )
        return versions

    # --- sync implementations --------------------------------------------

    def _save_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        custom_metadata: Optional[dict[str, Any]],
    ) -> int:
        artifact = ensure_part(artifact)
        prefix = self._get_key_prefix(app_name, user_id, filename, session_id)
        existing = self._versions_under(prefix)
        version = 0 if not existing else max(existing) + 1
        key = f"{prefix}/{version}"

        if artifact.inline_data:
            data = artifact.inline_data.data
            content_type = artifact.inline_data.mime_type or "application/octet-stream"
        elif artifact.text is not None:
            data = artifact.text.encode("utf-8")
            content_type = "text/plain"
        elif artifact.file_data:
            raise NotImplementedError(
                "Saving artifact with file_data is not supported in"
                " S3ArtifactService."
            )
        else:
            raise ValueError("Artifact must have either inline_data or text.")

        put_kwargs: dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if custom_metadata:
            # S3 user metadata values must be strings.
            put_kwargs["Metadata"] = {k: str(v) for k, v in custom_metadata.items()}
        self.s3_client.put_object(**put_kwargs)
        return version

    def _load_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int],
    ) -> Optional[types.Part]:
        if version is None:
            existing = self._versions_under(
                self._get_key_prefix(app_name, user_id, filename, session_id)
            )
            if not existing:
                return None
            version = max(existing)

        key = self._get_key(app_name, user_id, filename, version, session_id)
        try:
            resp = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return None
            raise
        data = resp["Body"].read()
        if not data:
            return None
        return types.Part.from_bytes(
            data=data, mime_type=resp.get("ContentType") or "application/octet-stream"
        )

    def _list_artifact_keys(
        self, app_name: str, user_id: str, session_id: Optional[str]
    ) -> list[str]:
        filenames: set[str] = set()

        if session_id:
            session_prefix = f"{self._prefix}{app_name}/{user_id}/{session_id}/"
            for key in self._list_keys(session_prefix):
                # key == session_prefix + <filename>/<version>
                rest = key[len(session_prefix) :]
                filename = "/".join(rest.split("/")[:-1])
                if filename:
                    filenames.add(filename)

        user_prefix = f"{self._prefix}{app_name}/{user_id}/user/"
        for key in self._list_keys(user_prefix):
            rest = key[len(user_prefix) :]
            filename = "/".join(rest.split("/")[:-1])
            if filename:
                filenames.add(filename)

        return sorted(filenames)

    def _delete_artifact(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> None:
        for version in self._versions_under(
            self._get_key_prefix(app_name, user_id, filename, session_id)
        ):
            key = self._get_key(app_name, user_id, filename, version, session_id)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)

    def _artifact_version_for_key(self, key: str, version: int) -> ArtifactVersion:
        head = self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
        last_modified = head.get("LastModified")
        return ArtifactVersion(
            version=version,
            canonical_uri=f"s3://{self.bucket_name}/{key}",
            create_time=last_modified.timestamp() if last_modified else 0.0,
            mime_type=head.get("ContentType"),
            custom_metadata=head.get("Metadata") or {},
        )

    def _list_artifact_versions(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
    ) -> list[ArtifactVersion]:
        prefix = self._get_key_prefix(app_name, user_id, filename, session_id)
        out: list[ArtifactVersion] = []
        for key in self._list_keys(f"{prefix}/"):
            tail = key.rsplit("/", 1)[-1]
            try:
                version = int(tail)
            except ValueError:
                continue
            out.append(self._artifact_version_for_key(key, version))
        out.sort(key=lambda av: av.version)
        return out

    def _get_artifact_version(
        self,
        app_name: str,
        user_id: str,
        session_id: Optional[str],
        filename: str,
        version: Optional[int],
    ) -> Optional[ArtifactVersion]:
        if version is None:
            existing = self._versions_under(
                self._get_key_prefix(app_name, user_id, filename, session_id)
            )
            if not existing:
                return None
            version = max(existing)
        key = self._get_key(app_name, user_id, filename, version, session_id)
        try:
            return self._artifact_version_for_key(key, version)
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return None
            raise

    # --- async API (BaseArtifactService) ----------------------------------

    async def save_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        artifact: Union[types.Part, dict[str, Any]],
        session_id: Optional[str] = None,
        custom_metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        return await asyncio.to_thread(
            self._save_artifact,
            app_name,
            user_id,
            session_id,
            filename,
            artifact,
            custom_metadata,
        )

    async def load_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[types.Part]:
        return await asyncio.to_thread(
            self._load_artifact, app_name, user_id, session_id, filename, version
        )

    async def list_artifact_keys(
        self, *, app_name: str, user_id: str, session_id: Optional[str] = None
    ) -> list[str]:
        return await asyncio.to_thread(
            self._list_artifact_keys, app_name, user_id, session_id
        )

    async def delete_artifact(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> None:
        return await asyncio.to_thread(
            self._delete_artifact, app_name, user_id, session_id, filename
        )

    async def list_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[int]:
        return await asyncio.to_thread(
            lambda: self._versions_under(
                self._get_key_prefix(app_name, user_id, filename, session_id)
            )
        )

    async def list_artifact_versions(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
    ) -> list[ArtifactVersion]:
        return await asyncio.to_thread(
            self._list_artifact_versions, app_name, user_id, session_id, filename
        )

    async def get_artifact_version(
        self,
        *,
        app_name: str,
        user_id: str,
        filename: str,
        session_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[ArtifactVersion]:
        return await asyncio.to_thread(
            self._get_artifact_version,
            app_name,
            user_id,
            session_id,
            filename,
            version,
        )


def _is_not_found(exc: Exception) -> bool:
    """True if a boto3 ClientError is a 404 / NoSuchKey / NotFound."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    err = resp.get("Error", {}) or {}
    code = str(err.get("Code", ""))
    status = (resp.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "404", "NotFound"} or status == 404


def register_s3_artifact_scheme() -> None:
    """Register the `s3://` artifact URI scheme with ADK's service registry.

    Call once at startup (before `get_fast_api_app`). Afterwards
    `ADK_CC_ARTIFACT_STORAGE_URI=s3://<bucket>/<optional-prefix>` resolves
    to an `S3ArtifactService`. Connection details come from the
    environment so the URI stays a plain bucket reference:
      - ADK_CC_S3_ENDPOINT_URL  — custom endpoint (MinIO/R2/Wasabi/…)
      - AWS_REGION / AWS_DEFAULT_REGION
      - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (boto3 default chain)
    Idempotent — re-registering the same scheme just overwrites it.
    """
    import os

    from google.adk.cli.service_registry import get_service_registry

    def s3_artifact_factory(uri: str, **_: Any) -> S3ArtifactService:
        parsed = urlparse(uri)
        bucket = parsed.netloc
        if not bucket:
            raise ValueError(
                f"s3:// artifact URI must include a bucket: got {uri!r}"
            )
        prefix = parsed.path.lstrip("/")
        return S3ArtifactService(
            bucket_name=bucket,
            prefix=prefix,
            endpoint_url=os.environ.get("ADK_CC_S3_ENDPOINT_URL")
            or os.environ.get("AWS_ENDPOINT_URL"),
            region_name=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION"),
        )

    get_service_registry().register_artifact_service("s3", s3_artifact_factory)
