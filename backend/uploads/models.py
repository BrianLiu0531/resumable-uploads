import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def default_expiry():
    return timezone.now() + timedelta(hours=settings.UPLOAD_TTL_HOURS)


class Upload(models.Model):
    """One tus upload. `offset` is the number of bytes durably stored and acknowledged to the client."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    filename = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    length = models.BigIntegerField()
    offset = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expiry, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # SHA-256 of the client IP with SECRET_KEY: enough for rate limits without storing addresses
    client = models.CharField(max_length=64, db_index=True)

    # Storage state. For S3: the multipart upload, its committed parts, and the bytes of an
    # unfinished part (< 5 MiB) kept as a separate object until enough data arrives.
    storage_key = models.CharField(max_length=512)
    s3_upload_id = models.CharField(max_length=1024, blank=True)
    parts = models.JSONField(default=list, blank=True)
    pending_key = models.CharField(max_length=512, blank=True)
    pending_size = models.BigIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["created_at"])]

    @property
    def completed(self) -> bool:
        return self.offset == self.length

    @property
    def expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def __str__(self) -> str:
        return f"{self.filename} ({self.offset}/{self.length})"
