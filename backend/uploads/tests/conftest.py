import base64
import os

import boto3
import pytest
from moto import mock_aws

from uploads.storage import get_storage

BUCKET = "test-uploads"


@pytest.fixture(params=["local", "s3"])
def storage(request, settings, tmp_path):
    """Run each test against local storage and against S3 (mocked by moto)."""
    get_storage.cache_clear()
    if request.param == "local":
        settings.UPLOAD_STORAGE = "local"
        settings.UPLOAD_DIR = tmp_path
        yield "local"
    else:
        os.environ.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="ap-southeast-2")
        with mock_aws():
            boto3.client("s3").create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"})
            settings.UPLOAD_STORAGE = "s3"
            settings.S3_BUCKET = BUCKET
            yield "s3"
    get_storage.cache_clear()


class Tus:
    """A small tus client over Django's test client."""

    def __init__(self, client):
        self.c = client

    def h(self, **extra):
        return {"HTTP_TUS_RESUMABLE": "1.0.0", **extra}

    def create(self, length: int, filename: str = "hello.bin", **extra):
        meta = f"filename {base64.b64encode(filename.encode()).decode()}"
        return self.c.post("/files/", **self.h(HTTP_UPLOAD_LENGTH=str(length), HTTP_UPLOAD_METADATA=meta, **extra))

    def new(self, length: int, **kw) -> str:
        resp = self.create(length, **kw)
        assert resp.status_code == 201, resp.content
        return resp["Location"].split("testserver")[-1]

    def patch(self, url: str, offset: int, data: bytes, **extra):
        return self.c.generic(
            "PATCH", url, data=data, content_type="application/offset+octet-stream", **self.h(HTTP_UPLOAD_OFFSET=str(offset), **extra)
        )

    def head(self, url: str):
        return self.c.head(url, **self.h())

    def delete(self, url: str):
        return self.c.delete(url, **self.h())

    def upload(self, data: bytes, chunks: list[int]) -> str:
        url = self.new(len(data))
        offset = 0
        for size in chunks:
            resp = self.patch(url, offset, data[offset : offset + size])
            assert resp.status_code == 204, resp.content
            offset = int(resp["Upload-Offset"])
        assert offset == len(data)
        return url


@pytest.fixture
def tus(client):
    return Tus(client)


def stored_bytes(storage_kind: str, upload) -> bytes:
    if storage_kind == "local":
        with get_storage().open(upload) as f:
            return f.read()
    return boto3.client("s3").get_object(Bucket=BUCKET, Key=upload.storage_key)["Body"].read()


def s3_keys() -> list[str]:
    return [o["Key"] for o in boto3.client("s3").list_objects_v2(Bucket=BUCKET).get("Contents", [])]
