"""Where upload bytes go: the local filesystem (development, tests) or S3 multipart uploads.

Every `write` is safe to repeat. If the process dies after writing bytes but before the database
commit that advances `Upload.offset`, the client retries from the old offset and the same bytes are
written to the same place again:

- local: bytes are written at their absolute position in the file;
- S3: part numbers are derived from the committed parts, and S3 replaces a re-uploaded part.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import BinaryIO, Protocol
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.db import transaction

from .models import Upload

log = logging.getLogger(__name__)

# S3 requires every part except the last to be at least 5 MiB
PART_SIZE = 5 * 1024 * 1024
COPY_BUFFER = 1024 * 1024


class Storage(Protocol):
    def create(self, upload: Upload) -> None:
        """Prepare storage for a new upload (sets storage fields on `upload`; the caller saves)."""

    def write(self, upload: Upload, data: BinaryIO, size: int) -> None:
        """Store `size` bytes from `data` at `upload.offset`. Finishes the file when it is complete."""

    def delete(self, upload: Upload) -> None:
        """Remove everything stored for the upload."""

    def download_url(self, upload: Upload) -> str | None:
        """A URL to download the finished file from, or None if the app should stream it."""


def content_disposition(filename: str) -> str:
    # Always download, never render: uploaded HTML must not run in anyone's browser
    return f"attachment; filename*=UTF-8''{quote(filename)}"


class LocalStorage:
    def __init__(self, root: os.PathLike):
        self.root = os.fspath(root)

    def path(self, upload: Upload) -> str:
        return os.path.join(self.root, upload.storage_key)

    def create(self, upload: Upload) -> None:
        upload.storage_key = str(upload.id)
        os.makedirs(self.root, exist_ok=True)
        open(self.path(upload), "wb").close()

    def write(self, upload: Upload, data: BinaryIO, size: int) -> None:
        with open(self.path(upload), "r+b") as f:
            f.seek(upload.offset)
            remaining = size
            while remaining:
                chunk = data.read(min(COPY_BUFFER, remaining))
                if not chunk:
                    raise IOError("request body ended early")
                f.write(chunk)
                remaining -= len(chunk)
            f.truncate()  # drop anything a failed earlier attempt left beyond this point

    def delete(self, upload: Upload) -> None:
        try:
            os.remove(self.path(upload))
        except FileNotFoundError:
            pass

    def download_url(self, upload: Upload) -> str | None:
        return None

    def open(self, upload: Upload) -> BinaryIO:
        return open(self.path(upload), "rb")


class S3Storage:
    def __init__(self, bucket: str, prefix: str, region: str, client=None):
        self.bucket = bucket
        self.prefix = prefix
        # SigV4 for presigned URLs (the older SigV2 is deprecated and missing in newer regions)
        self.s3 = client or boto3.client("s3", region_name=region, config=Config(signature_version="s3v4"))

    def create(self, upload: Upload) -> None:
        upload.storage_key = f"{self.prefix}{upload.id}"
        extra = {
            "ContentType": "application/octet-stream",
            "ContentDisposition": content_disposition(upload.filename),
        }
        if upload.length == 0:
            self.s3.put_object(Bucket=self.bucket, Key=upload.storage_key, Body=b"", **extra)
            return
        resp = self.s3.create_multipart_upload(Bucket=self.bucket, Key=upload.storage_key, **extra)
        upload.s3_upload_id = resp["UploadId"]

    def write(self, upload: Upload, data: BinaryIO, size: int) -> None:
        final = upload.offset + size == upload.length
        parts = list(upload.parts)
        old_pending = upload.pending_key

        # Bytes acknowledged earlier that did not fill a part yet come first
        buf = bytearray()
        if upload.pending_key:
            obj = self.s3.get_object(Bucket=self.bucket, Key=upload.pending_key)
            buf += obj["Body"].read()
            if len(buf) != upload.pending_size:
                raise IOError(f"pending part has {len(buf)} bytes, expected {upload.pending_size}")

        try:
            remaining = size
            while True:
                chunk = data.read(min(PART_SIZE - len(buf), remaining))
                if remaining and not chunk:
                    raise IOError("request body ended early")
                buf += chunk
                remaining -= len(chunk)
                if len(buf) == PART_SIZE:
                    parts.append(self._upload_part(upload, len(parts) + 1, bytes(buf)))
                    buf.clear()
                if remaining == 0:
                    break

            if final:
                if buf:
                    parts.append(self._upload_part(upload, len(parts) + 1, bytes(buf)))
                self.s3.complete_multipart_upload(
                    Bucket=self.bucket,
                    Key=upload.storage_key,
                    UploadId=upload.s3_upload_id,
                    MultipartUpload={"Parts": [{"PartNumber": p["PartNumber"], "ETag": p["ETag"]} for p in parts]},
                )
                upload.pending_key, upload.pending_size = "", 0
            elif buf:
                # A new key per offset: the previous pending object stays intact until the
                # database commit, so a crash before it leaves a consistent state to retry from
                upload.pending_key = f"{upload.storage_key}.part.{upload.offset + size}"
                upload.pending_size = len(buf)
                self.s3.put_object(Bucket=self.bucket, Key=upload.pending_key, Body=bytes(buf))
            else:
                upload.pending_key, upload.pending_size = "", 0
        except ClientError as e:
            if final and e.response["Error"]["Code"] == "NoSuchUpload" and self._already_complete(upload):
                # A previous attempt completed the object but died before the database commit
                upload.pending_key, upload.pending_size = "", 0
                parts = upload.parts
            else:
                raise

        upload.parts = parts
        if old_pending and old_pending != upload.pending_key:
            transaction.on_commit(lambda: self._delete_quietly(old_pending))

    def _upload_part(self, upload: Upload, number: int, body: bytes) -> dict:
        resp = self.s3.upload_part(
            Bucket=self.bucket, Key=upload.storage_key, UploadId=upload.s3_upload_id, PartNumber=number, Body=body
        )
        return {"PartNumber": number, "ETag": resp["ETag"], "Size": len(body)}

    def _already_complete(self, upload: Upload) -> bool:
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=upload.storage_key)
        except ClientError:
            return False
        return head["ContentLength"] == upload.length

    def _delete_quietly(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as e:  # the bucket lifecycle rule removes stragglers anyway
            log.warning("could not delete %s: %s", key, e)

    def delete(self, upload: Upload) -> None:
        if upload.s3_upload_id and not upload.completed:
            try:
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=upload.storage_key, UploadId=upload.s3_upload_id)
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchUpload":
                    raise
        # The object itself plus any pending-part objects ("<key>.part.<offset>")
        keys = [{"Key": upload.storage_key}]
        listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"{upload.storage_key}.part.")
        keys += [{"Key": o["Key"]} for o in listing.get("Contents", [])]
        self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})

    def download_url(self, upload: Upload) -> str | None:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": upload.storage_key,
                "ResponseContentDisposition": content_disposition(upload.filename),
            },
            ExpiresIn=300,
        )


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    if settings.UPLOAD_STORAGE == "s3":
        if not settings.S3_BUCKET:
            raise RuntimeError("S3_BUCKET must be set when UPLOAD_STORAGE=s3")
        return S3Storage(settings.S3_BUCKET, settings.S3_PREFIX, settings.S3_REGION)
    return LocalStorage(settings.UPLOAD_DIR)
