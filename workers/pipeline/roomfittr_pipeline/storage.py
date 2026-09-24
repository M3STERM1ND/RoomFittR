"""Artefact storage for the pipeline (implementation-plan.md 2.4, 1.1).

The pipeline writes frames, point clouds, reconstructions and GLBs, and the
web app later reads them. 2.4 fixes the key layout:

    rooms/{room_id}/scans/{scan_id}/{pipeline_version}/{stage}/...

Two backends sit behind one protocol, and the choice is made by whether R2
credentials exist rather than by a flag:

- **`S3ObjectStore`** talks to Cloudflare R2 over the S3 API. R2 is chosen in
  1.1 for zero egress, which matters because GLBs are downloaded constantly.
- **`LocalObjectStore`** writes to a directory. It is not a mock: the Tier A
  bake-off runs on this machine against local ARKitScenes files and has no
  reason to push 12 GB through a network, and CI has no credentials at all.

Keeping both behind `ObjectStore` means the evaluation harness and the Modal
worker run the *same* code path, so "it worked locally" is evidence about the
pipeline rather than about which storage it happened to use.

**Buckets are private.** Room videos are the inside of someone's home (1.1's
second hard rule), so nothing here makes an object public; browser access is a
presigned URL with a short expiry, which is what `presign_get` produces.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .errors import PipelineError, Stage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

# 1.1: presigned URLs are short-lived. Long enough for a browser to fetch a
# GLB on a slow connection, short enough that a leaked URL in a log or a
# referrer header has expired by the time anyone reads it.
DEFAULT_PRESIGN_EXPIRY_S = 3600

# The stage segment of a key is `Stage`, not a free-form string. `errors.py`
# already states that its values are "what lands in `scans.stage`, in R2
# artefact keys and in the UI progress label", so accepting anything else here
# would let an artefact be written to a prefix the lifecycle rules never
# expire -- and the symptom of that is a storage bill, not an error.
STAGES = frozenset(stage.value for stage in Stage)


def artefact_key(
    *,
    room_id: str,
    scan_id: str,
    pipeline_version: str,
    stage: Stage | str,
    filename: str,
) -> str:
    """Build the 2.4 key for one artefact.

    Validated rather than formatted blindly: a key is the only thing tying an
    artefact to the scan it belongs to, and a stray separator or an empty id
    produces a key that reads fine and points nowhere.
    """
    stage_value = stage.value if isinstance(stage, Stage) else str(stage)
    if stage_value not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(STAGES)}")
    segments = {"room_id": room_id, "scan_id": scan_id, "pipeline_version": pipeline_version}
    for name, value in segments.items():
        if not value or "/" in value or value in {".", ".."}:
            raise ValueError(f"{name} must be a non-empty path segment, got {value!r}")
    if not filename or filename.startswith("/"):
        raise ValueError(f"filename must be relative and non-empty, got {filename!r}")
    return (
        f"rooms/{room_id}/scans/{scan_id}/{pipeline_version}/{stage_value}/"
        f"{PurePosixPath(filename).as_posix()}"
    )


@runtime_checkable
class ObjectStore(Protocol):
    """What the pipeline needs of storage. Deliberately small."""

    name: str

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None: ...

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...

    def get_file(self, key: str, destination: Path) -> Path: ...

    def get_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str) -> Iterator[str]: ...

    def presign_get(self, key: str, *, expires_in: int = DEFAULT_PRESIGN_EXPIRY_S) -> str: ...


@dataclass(slots=True)
class LocalObjectStore:
    """Filesystem-backed store rooted at `root`.

    `presign_get` returns a `file://` URL. That is honest about what it is: no
    expiry is enforced because the filesystem has no such concept, and a
    caller that needs a real expiry needs real R2.
    """

    root: Path
    name: str = "local"

    def _path(self, key: str) -> Path:
        # Keys are ours, but a traversal here would write outside the run
        # directory, so it is checked rather than assumed.
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            raise ValueError(f"key {key!r} escapes the store root")
        return candidate

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get_file(self, key: str, destination: Path) -> Path:
        source = self._path(key)
        if not source.is_file():
            raise PipelineError("INTERNAL", Stage.INGEST, f"missing artefact {key!r}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination

    def get_bytes(self, key: str) -> bytes:
        source = self._path(key)
        if not source.is_file():
            raise PipelineError("INTERNAL", Stage.INGEST, f"missing artefact {key!r}")
        return source.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str) -> Iterator[str]:
        base = self.root.resolve()
        if not base.exists():
            return iter(())
        keys = sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())
        return iter([k for k in keys if k.startswith(prefix)])

    def presign_get(self, key: str, *, expires_in: int = DEFAULT_PRESIGN_EXPIRY_S) -> str:
        return self._path(key).as_uri()


@dataclass(slots=True)
class S3ObjectStore:
    """Cloudflare R2 over the S3 API.

    boto3 is imported lazily so the pipeline keeps working -- and CI keeps
    passing -- on a machine that has no credentials and no reason to install
    an AWS SDK.
    """

    bucket: str
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    name: str = "r2"
    _client: Any = field(default=None, repr=False)

    def _c(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - dependency of the worker image
                raise PipelineError(
                    "INTERNAL", Stage.INGEST, "boto3 is required for R2 storage"
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
            )
        return self._client

    def put_file(self, key: str, source: Path, *, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else None
        self._c().upload_file(str(source), self.bucket, key, ExtraArgs=extra)

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        kwargs: dict[str, Any] = {"ContentType": content_type} if content_type else {}
        self._c().put_object(Bucket=self.bucket, Key=key, Body=data, **kwargs)

    def get_file(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._c().download_file(self.bucket, key, str(destination))
        return destination

    def get_bytes(self, key: str) -> bytes:
        response = self._c().get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._c().head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def list(self, prefix: str) -> Iterator[str]:
        paginator = self._c().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield str(item["Key"])

    def presign_get(self, key: str, *, expires_in: int = DEFAULT_PRESIGN_EXPIRY_S) -> str:
        return str(
            self._c().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        )


def from_env(*, local_root: Path | None = None) -> ObjectStore:
    """Pick a store from the environment.

    R2 when all four credentials are present and the endpoint is real,
    otherwise local. The placeholder endpoint that ships in `.env.example`
    is treated as absent, because a half-filled `.env.local` should fall back
    rather than fail with a DNS error three stages into a run.
    """
    endpoint = os.environ.get("R2_ENDPOINT", "")
    required = (
        os.environ.get("R2_ACCOUNT_ID"),
        os.environ.get("R2_ACCESS_KEY_ID"),
        os.environ.get("R2_SECRET_ACCESS_KEY"),
        os.environ.get("R2_BUCKET"),
    )
    configured = all(required) and bool(endpoint) and "REPLACE" not in endpoint
    if configured:
        return S3ObjectStore(
            bucket=str(os.environ["R2_BUCKET"]),
            endpoint_url=endpoint,
            access_key_id=str(os.environ["R2_ACCESS_KEY_ID"]),
            secret_access_key=str(os.environ["R2_SECRET_ACCESS_KEY"]),
        )
    root = local_root or Path(os.environ.get("ROOMFITTR_ARTEFACT_ROOT", "eval/runs/local"))
    return LocalObjectStore(root=root)
