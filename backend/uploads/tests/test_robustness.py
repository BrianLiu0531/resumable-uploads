"""Failure modes: disconnects, concurrent writers, crashes between storage and database, abuse limits."""

import os
import threading
import time
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from django.db import connection
from django.test import Client, RequestFactory

from uploads import storage as storage_module
from uploads.models import Upload
from uploads.tus import TusView
from uploads.util import clean_filename

from .conftest import s3_keys, stored_bytes

pytestmark = pytest.mark.django_db(transaction=True)

MiB = 1024 * 1024


def upload_for(url: str) -> Upload:
    return Upload.objects.get(pk=url.rsplit("/", 1)[-1])


class DisconnectingStream:
    """A request body whose client goes away after `limit` bytes."""

    def __init__(self, data: bytes, limit: int):
        self.data, self.limit, self.pos = data, limit, 0

    def read(self, n=-1):
        if self.pos >= self.limit:
            raise OSError("client disconnected")
        chunk = self.data[self.pos : min(self.limit, self.pos + n)]
        self.pos += len(chunk)
        return chunk


def test_keeps_the_bytes_that_arrived_before_a_disconnect(tus, storage):
    data = os.urandom(8 * MiB)
    url = tus.new(len(data))
    request = RequestFactory().generic(
        "PATCH", url, data=data, content_type="application/offset+octet-stream",
        HTTP_TUS_RESUMABLE="1.0.0", HTTP_UPLOAD_OFFSET="0",
    )
    request._stream = DisconnectingStream(data, limit=6 * MiB + 123)

    resp = TusView.as_view()(request, upload_id=upload_for(url).pk)
    assert resp.status_code == 204 and resp["Upload-Offset"] == str(6 * MiB + 123)

    # The client reconnects, asks where to continue, and finishes
    offset = int(tus.head(url)["Upload-Offset"])
    assert tus.patch(url, offset, data[offset:]).status_code == 204
    assert stored_bytes(storage, upload_for(url)) == data


def test_a_disconnect_with_a_checksum_stores_nothing(tus, storage):
    url = tus.new(10)
    request = RequestFactory().generic(
        "PATCH", url, data=b"helloworld", content_type="application/offset+octet-stream",
        HTTP_TUS_RESUMABLE="1.0.0", HTTP_UPLOAD_OFFSET="0", HTTP_UPLOAD_CHECKSUM="sha1 AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    )
    request._stream = DisconnectingStream(b"helloworld", limit=4)
    assert TusView.as_view()(request, upload_id=upload_for(url).pk).status_code == 400
    assert upload_for(url).offset == 0


def test_concurrent_writers_cannot_both_append(tus, storage):
    """Two requests send the same chunk at the same moment: exactly one is stored."""
    data = os.urandom(3 * MiB)
    url = tus.new(len(data))
    real_write = storage_module.get_storage().write

    def slow_write(*args, **kwargs):
        time.sleep(0.5)  # hold the row lock long enough for the other request to arrive
        return real_write(*args, **kwargs)

    statuses = []

    def send():
        try:
            resp = tus.__class__(Client()).patch(url, 0, data)
            statuses.append(resp.status_code)
        finally:
            connection.close()

    with mock.patch.object(storage_module.get_storage(), "write", side_effect=slow_write):
        threads = [threading.Thread(target=send) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(statuses)[0] == 204 and sorted(statuses)[1] in (409, 423), statuses
    upload = upload_for(url)
    assert upload.offset == len(data)
    assert stored_bytes(storage, upload) == data  # not data + data


def test_retry_after_a_crash_between_storage_and_commit(tus, storage):
    """Bytes reach storage, then the database commit fails: the client's retry must not corrupt the file."""
    data = os.urandom(13 * MiB)
    url = tus.new(len(data))
    assert tus.patch(url, 0, data[: 6 * MiB]).status_code == 204

    with mock.patch.object(Upload, "save", side_effect=RuntimeError("database went away")):
        with pytest.raises(RuntimeError):
            tus.patch(url, 6 * MiB, data[6 * MiB : 11 * MiB])
    assert upload_for(url).offset == 6 * MiB  # nothing was committed

    # The client retries the same chunk, then finishes
    assert tus.patch(url, 6 * MiB, data[6 * MiB : 11 * MiB]).status_code == 204
    assert tus.patch(url, 11 * MiB, data[11 * MiB :]).status_code == 204
    upload = upload_for(url)
    assert stored_bytes(storage, upload) == data
    if storage == "s3":
        assert [p["PartNumber"] for p in upload.parts] == [1, 2, 3]
        assert s3_keys() == [upload.storage_key]


def test_retry_after_a_crash_between_completion_and_commit(tus, storage):
    """S3 finished the object, then the commit failed: the retry recognises the finished object."""
    if storage != "s3":
        pytest.skip("specific to S3 multipart completion")
    data = os.urandom(6 * MiB)
    url = tus.new(len(data))
    with mock.patch.object(Upload, "save", side_effect=RuntimeError("database went away")):
        with pytest.raises(RuntimeError):
            tus.patch(url, 0, data)

    # Real S3 answers NoSuchUpload for parts of a completed multipart upload (moto raises internally)
    s3 = storage_module.get_storage().s3
    no_such_upload = ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, "UploadPart")
    with mock.patch.object(s3, "upload_part", side_effect=no_such_upload):
        assert tus.patch(url, 0, data).status_code == 204
    assert stored_bytes(storage, upload_for(url)) == data


def test_rate_limits_per_client(tus, storage, settings):
    settings.UPLOADS_PER_IP_PER_HOUR = 2
    assert tus.create(1, REMOTE_ADDR="10.0.0.1").status_code == 201
    assert tus.create(1, REMOTE_ADDR="10.0.0.1").status_code == 201
    limited = tus.create(1, REMOTE_ADDR="10.0.0.1")
    assert limited.status_code == 429 and limited["Retry-After"]
    assert tus.create(1, REMOTE_ADDR="10.0.0.2").status_code == 201
    # The real client address comes first in X-Forwarded-For behind Render's proxy
    assert tus.create(1, HTTP_X_FORWARDED_FOR="10.0.0.1, 172.16.0.1").status_code == 429


def test_daily_quota_caps_total_storage(tus, storage, settings):
    settings.DAILY_BYTE_QUOTA = 10 * MiB
    assert tus.create(6 * MiB).status_code == 201
    assert tus.create(5 * MiB).status_code == 429
    assert tus.create(4 * MiB).status_code == 201


def test_client_addresses_are_not_stored(tus, storage):
    tus.create(1, REMOTE_ADDR="203.0.113.7")
    assert "203.0.113.7" not in Upload.objects.get().client


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\photo.jpg", "photo.jpg"),
        ("bad\x00name\n.txt", "badname.txt"),
        ("..", "upload"),
        ("x" * 300, "x" * 255),
    ],
)
def test_filenames_are_sanitised(raw, clean):
    assert clean_filename(raw) == clean
