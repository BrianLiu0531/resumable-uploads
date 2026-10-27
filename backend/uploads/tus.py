"""The tus 1.0.0 resumable upload protocol: core plus the creation, expiration, termination and
checksum extensions. https://tus.io/protocols/resumable-upload

PATCH is the interesting part. The request body is first received into a temporary file (outside
any transaction, so a slow client never holds a database lock). Then, inside one transaction:

1. the upload row is locked with SELECT ... FOR UPDATE NOWAIT, so only one writer per upload;
2. the client's Upload-Offset must equal the stored offset, otherwise 409 (it is out of date);
3. the bytes are written to storage at that offset, and the new offset is committed.

Two requests racing with the same offset therefore cannot both append: one wins, the other gets
409 or 423 and asks the server for the current offset (HEAD) before retrying.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import tempfile
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import http_date
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import Upload
from .storage import get_storage
from .util import cleanup_expired, client_id, clean_filename

log = logging.getLogger(__name__)

TUS_VERSION = "1.0.0"
EXTENSIONS = "creation,expiration,termination,checksum"
CHECKSUMS = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256}
CHECKSUM_MISMATCH = 460  # defined by the tus checksum extension


def is_lock_conflict(e: DatabaseError) -> bool:
    # PostgreSQL 55P03 lock_not_available, raised by FOR UPDATE NOWAIT
    return getattr(e.__cause__, "sqlstate", None) == "55P03"


def tus_response(status: int, body: str = "", **headers: str) -> HttpResponse:
    resp = HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")
    resp["Tus-Resumable"] = TUS_VERSION
    resp["Cache-Control"] = "no-store"
    for k, v in headers.items():
        resp[k.replace("_", "-")] = v
    return resp


def parse_metadata(raw: str) -> dict[str, str]:
    """Upload-Metadata: comma-separated "key base64value" pairs (the value may be omitted)."""
    meta: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        key, _, value = pair.partition(" ")
        if not key or " " in value.strip():
            raise ValueError(f"malformed pair {pair!r}")
        meta[key] = base64.b64decode(value, validate=True).decode("utf-8") if value else ""
    return meta


def encode_metadata(meta: dict[str, str]) -> str:
    return ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" if v else k for k, v in meta.items())


def parse_checksum(raw: str):
    algo, _, digest = raw.strip().partition(" ")
    if algo not in CHECKSUMS:
        raise ValueError(f"unsupported checksum algorithm {algo!r}")
    return CHECKSUMS[algo], base64.b64decode(digest, validate=True)


@method_decorator(csrf_exempt, name="dispatch")
class TusView(View):
    http_method_names = ["options", "post", "head", "patch", "delete"]

    def dispatch(self, request: HttpRequest, *args, **kwargs):
        if request.method != "OPTIONS" and request.headers.get("Tus-Resumable") != TUS_VERSION:
            return tus_response(412, "unsupported tus version", Tus_Version=TUS_VERSION)
        return super().dispatch(request, *args, **kwargs)

    def options(self, request, upload_id=None):
        return tus_response(
            204,
            Tus_Version=TUS_VERSION,
            Tus_Extension=EXTENSIONS,
            Tus_Max_Size=str(settings.MAX_UPLOAD_SIZE),
            Tus_Checksum_Algorithm=",".join(CHECKSUMS),
        )

    # ---- creation ----

    def post(self, request, upload_id=None):
        if upload_id is not None:
            return tus_response(405)
        if "Upload-Defer-Length" in request.headers:
            return tus_response(400, "deferred length is not supported")
        try:
            length = int(request.headers["Upload-Length"])
            if length < 0:
                raise ValueError
        except (KeyError, ValueError):
            return tus_response(400, "Upload-Length must be a non-negative integer")
        if length > settings.MAX_UPLOAD_SIZE:
            return tus_response(413, f"files are limited to {settings.MAX_UPLOAD_SIZE} bytes")
        try:
            metadata = parse_metadata(request.headers.get("Upload-Metadata", ""))
        except (ValueError, binascii.Error, UnicodeDecodeError) as e:
            return tus_response(400, f"invalid Upload-Metadata: {e}")

        client = client_id(request)
        now = timezone.now()
        if Upload.objects.filter(client=client, created_at__gte=now - timedelta(hours=1)).count() >= settings.UPLOADS_PER_IP_PER_HOUR:
            return tus_response(429, "too many uploads from this address; try again later", Retry_After="3600")
        recent = Upload.objects.filter(created_at__gte=now - timedelta(hours=24)).aggregate(total=Sum("length"))["total"] or 0
        if recent + length > settings.DAILY_BYTE_QUOTA:
            return tus_response(429, "the demo's daily upload quota is used up; try again tomorrow", Retry_After="3600")

        cleanup_expired(limit=20)  # keeps storage bounded without a scheduler

        upload = Upload(
            filename=clean_filename(metadata.get("filename") or metadata.get("name") or "upload"),
            metadata=metadata,
            length=length,
            client=client,
        )
        get_storage().create(upload)
        if length == 0:
            upload.completed_at = now
        upload.save()
        log.info("created upload %s (%d bytes)", upload.id, length)
        return tus_response(
            201,
            Location=request.build_absolute_uri(f"/files/{upload.id}"),
            Upload_Offset="0",
            Upload_Expires=http_date(upload.expires_at.timestamp()),
        )

    # ---- core ----

    def head(self, request, upload_id=None):
        upload = Upload.objects.filter(pk=upload_id).first()
        if upload is None:
            return tus_response(404)
        if upload.expired:
            return tus_response(410)
        headers = {
            "Upload_Offset": str(upload.offset),
            "Upload_Length": str(upload.length),
            "Upload_Expires": http_date(upload.expires_at.timestamp()),
        }
        if upload.metadata:
            headers["Upload_Metadata"] = encode_metadata(upload.metadata)
        return tus_response(200, **headers)

    def patch(self, request, upload_id=None):
        if request.content_type != "application/offset+octet-stream":
            return tus_response(415, "Content-Type must be application/offset+octet-stream")
        try:
            client_offset = int(request.headers["Upload-Offset"])
        except (KeyError, ValueError):
            return tus_response(400, "Upload-Offset must be an integer")
        try:
            size = int(request.META["CONTENT_LENGTH"])
        except (KeyError, ValueError):
            return tus_response(411, "Content-Length is required")
        if size > settings.MAX_CHUNK_SIZE:
            return tus_response(413, f"send at most {settings.MAX_CHUNK_SIZE} bytes per request")
        checksum = None
        if "Upload-Checksum" in request.headers:
            try:
                checksum = parse_checksum(request.headers["Upload-Checksum"])
            except (ValueError, binascii.Error) as e:
                return tus_response(400, str(e))

        # Receive the body first. If the client disconnects, keep what arrived (that is the point
        # of a resumable upload) unless a checksum was promised, which a partial body cannot match.
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as body:
            hasher = checksum[0]() if checksum else None
            received = 0
            try:
                while received < size:
                    chunk = request.read(min(1024 * 1024, size - received))
                    if not chunk:
                        break
                    body.write(chunk)
                    received += len(chunk)
                    if hasher:
                        hasher.update(chunk)
            except OSError as e:
                log.info("upload %s: client disconnected after %d of %d bytes (%s)", upload_id, received, size, e)
            if checksum and received < size:
                return tus_response(400, "incomplete request body")
            if checksum and hasher.digest() != checksum[1]:
                return tus_response(CHECKSUM_MISMATCH, "checksum mismatch")
            if received == 0:
                return tus_response(400, "empty request body")
            body.seek(0)
            return self._append(upload_id, client_offset, body, received)

    def _append(self, upload_id, client_offset: int, body, size: int) -> HttpResponse:
        try:
            with transaction.atomic():
                upload = Upload.objects.select_for_update(nowait=True).filter(pk=upload_id).first()
                if upload is None:
                    return tus_response(404)
                if upload.expired:
                    return tus_response(410)
                if client_offset != upload.offset:
                    return tus_response(409, f"offset is {upload.offset}", Upload_Offset=str(upload.offset))
                if upload.offset + size > upload.length:
                    return tus_response(413, "body exceeds Upload-Length")

                get_storage().write(upload, body, size)
                upload.offset += size
                if upload.completed:
                    upload.completed_at = timezone.now()
                    log.info("completed upload %s", upload.id)
                upload.save()
        except DatabaseError as e:
            if is_lock_conflict(e):
                return tus_response(423, "another request is writing to this upload")
            raise
        return tus_response(204, Upload_Offset=str(upload.offset), Upload_Expires=http_date(upload.expires_at.timestamp()))

    # ---- termination ----

    def delete(self, request, upload_id=None):
        if upload_id is None:
            return tus_response(405)
        try:
            with transaction.atomic():
                upload = Upload.objects.select_for_update(nowait=True).filter(pk=upload_id).first()
                if upload is None:
                    return tus_response(404)
                get_storage().delete(upload)
                upload.delete()
        except DatabaseError as e:
            if is_lock_conflict(e):
                return tus_response(423, "another request is writing to this upload")
            raise
        return tus_response(204)
