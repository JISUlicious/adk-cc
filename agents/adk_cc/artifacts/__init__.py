"""adk-cc artifact service implementations.

ADK ships InMemory / File / GCS artifact services but no S3. This package
adds `S3ArtifactService` for AWS S3 and any S3-compatible object store
(MinIO, Cloudflare R2, Wasabi, Backblaze B2, Ceph RGW, DigitalOcean
Spaces). It registers itself as the `s3://` URI scheme so the existing
`ADK_CC_ARTIFACT_STORAGE_URI` wiring picks it up — see
`register_s3_artifact_scheme()`.
"""

from .s3_artifact_service import S3ArtifactService, register_s3_artifact_scheme

__all__ = ["S3ArtifactService", "register_s3_artifact_scheme"]
