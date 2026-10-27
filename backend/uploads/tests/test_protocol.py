"""tus 1.0 protocol behaviour, against both storage backends."""

import base64
import hashlib
import os
from datetime import timedelta

import pytest
from django.utils import timezone

from uploads.models import Upload
from uploads.util import cleanup_expired

from .conftest import s3_keys, stored_bytes

pytestmark = pytest.mark.django_db(transaction=True)

MiB = 1024 * 1024


def upload_for(url: str) -> Upload:
    return Upload.objects.get(pk=url.rsplit("/", 1)[-1])


def test_options_advertises_the_server(tus, client):
    resp = client.options("/files/")
    assert resp.status_code == 204
    assert resp["Tus-Version"] == "1.0.0"
    assert set(resp["Tus-Extension"].split(",")) == {"creation", "expiration", "termination", "checksum"}
    assert "sha256" in resp["Tus-Checksum-Algorithm"]


def test_requires_the_tus_resumable_header(client):
    resp = client.post("/files/", HTTP_UPLOAD_LENGTH="5")
    assert resp.status_code == 412
    assert resp["Tus-Version"] == "1.0.0"


@pytest.mark.parametrize(
    "headers, status",
    [
        ({}, 400),  # no Upload-Length
        ({"HTTP_UPLOAD_LENGTH": "-1"}, 400),
        ({"HTTP_UPLOAD_LENGTH": "abc"}, 400),
        ({"HTTP_UPLOAD_DEFER_LENGTH": "1"}, 400),
        ({"HTTP_UPLOAD_LENGTH": str(51 * MiB)}, 413),
        ({"HTTP_UPLOAD_LENGTH": "5", "HTTP_UPLOAD_METADATA": "filename not-base64!"}, 400),
    ],
)
def test_rejects_invalid_creation(tus, client, storage, headers, status):
    assert client.post("/files/", **tus.h(**headers)).status_code == status


@pytest.mark.parametrize("chunks", [[3 * MiB, 4 * MiB + 1, 5 * MiB + 2], [12 * MiB + 3], [5 * MiB, 5 * MiB, 2 * MiB + 3]])
def test_reassembles_the_file_from_any_chunking(tus, storage, chunks):
    data = os.urandom(sum(chunks))
    url = tus.upload(data, chunks)
    upload = upload_for(url)
    assert upload.completed and upload.completed_at is not None
    assert stored_bytes(storage, upload) == data
    if storage == "s3":
        # Every part but the last is exactly 5 MiB, and no pending-part objects are left behind
        assert all(p["Size"] == 5 * MiB for p in upload.parts[:-1])
        assert s3_keys() == [upload.storage_key]


def test_head_reports_progress_for_resuming(tus, storage):
    data = os.urandom(7 * MiB)
    url = tus.new(len(data), filename="report.pdf")
    tus.patch(url, 0, data[: 2 * MiB])

    resp = tus.head(url)
    assert resp.status_code == 200
    assert resp["Upload-Offset"] == str(2 * MiB)
    assert resp["Upload-Length"] == str(len(data))
    assert resp["Cache-Control"] == "no-store"
    assert base64.b64decode(resp["Upload-Metadata"].split(" ")[1]) == b"report.pdf"

    # A new client picks up where the server says it is
    assert tus.patch(url, int(resp["Upload-Offset"]), data[2 * MiB :]).status_code == 204
    assert stored_bytes(storage, upload_for(url)) == data


def test_rejects_out_of_date_offsets_and_bad_patches(tus, storage):
    url = tus.new(10)
    assert tus.patch(url, 0, b"hello").status_code == 204

    stale = tus.patch(url, 0, b"hello")  # a retry of a request that already succeeded
    assert stale.status_code == 409 and stale["Upload-Offset"] == "5"
    assert tus.patch(url, 5, b"too many bytes").status_code == 413
    wrong_type = tus.c.generic("PATCH", url, data=b"x", content_type="application/json", **tus.h(HTTP_UPLOAD_OFFSET="5"))
    assert wrong_type.status_code == 415
    assert tus.patch("/files/00000000-0000-0000-0000-000000000000", 0, b"x").status_code == 404
    assert upload_for(url).offset == 5


def test_verifies_checksums(tus, storage):
    url = tus.new(10)
    good = base64.b64encode(hashlib.sha1(b"hello").digest()).decode()
    assert tus.patch(url, 0, b"hello", HTTP_UPLOAD_CHECKSUM=f"sha1 {good}").status_code == 204

    bad = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
    assert tus.patch(url, 5, b"world", HTTP_UPLOAD_CHECKSUM=f"sha256 {bad}").status_code == 460
    assert tus.patch(url, 5, b"world", HTTP_UPLOAD_CHECKSUM=f"crc32 {good}").status_code == 400
    assert upload_for(url).offset == 5  # the corrupted chunk was not stored


def test_termination_removes_everything(tus, storage):
    url = tus.new(9 * MiB)
    tus.patch(url, 0, os.urandom(6 * MiB))  # one full S3 part plus a pending part
    assert tus.delete(url).status_code == 204
    assert tus.head(url).status_code == 404
    if storage == "s3":
        import boto3

        assert s3_keys() == []
        assert boto3.client("s3").list_multipart_uploads(Bucket="test-uploads").get("Uploads", []) == []


def test_expired_uploads_are_gone_and_cleaned_up(tus, storage):
    url = tus.new(10)
    tus.patch(url, 0, b"hello")
    Upload.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    assert tus.head(url).status_code == 410
    assert tus.patch(url, 5, b"world").status_code == 410
    assert cleanup_expired() == 1
    assert Upload.objects.count() == 0
    if storage == "s3":
        assert s3_keys() == []


def test_empty_files_complete_on_creation(tus, client, storage):
    url = tus.new(0)
    upload = upload_for(url)
    assert upload.completed
    assert stored_bytes(storage, upload) == b""


def test_method_override_for_proxies_that_block_patch(tus, storage):
    url = tus.new(5)
    resp = tus.c.post(
        url, data=b"hello", content_type="application/offset+octet-stream",
        **tus.h(HTTP_UPLOAD_OFFSET="0", HTTP_X_HTTP_METHOD_OVERRIDE="PATCH"),
    )
    assert resp.status_code == 204 and upload_for(url).completed


def test_download(tus, client, storage):
    data = os.urandom(1000)
    url = tus.upload(data, [400, 600])
    resp = client.get(f"/api/uploads/{upload_for(url).id}/download")
    if storage == "local":
        assert resp.status_code == 200 and b"".join(resp.streaming_content) == data
        assert resp["Content-Disposition"].startswith("attachment;")
    else:
        assert resp.status_code == 302 and "X-Amz-Signature" in resp["Location"]
