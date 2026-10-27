import hashlib
import logging
import re
import unicodedata

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)


def client_id(request) -> str:
    """A stable, non-reversible ID for the client's IP (Render puts the real IP first in X-Forwarded-For)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()


def clean_filename(name: str) -> str:
    """Keep only the base name, without control characters, at most 255 characters."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in unicodedata.normalize("NFC", name) if unicodedata.category(c)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:255] or "upload"


def cleanup_expired(limit: int | None = None) -> int:
    """Delete uploads past their expiry, with their stored bytes. Returns how many were removed."""
    from .models import Upload
    from .storage import get_storage

    storage = get_storage()
    removed = 0
    qs = Upload.objects.filter(expires_at__lte=timezone.now()).order_by("expires_at")
    for upload in qs[:limit] if limit else qs.iterator():
        try:
            storage.delete(upload)
            upload.delete()
            removed += 1
        except Exception:  # a storage hiccup must not break the request that triggered cleanup
            log.exception("could not clean up upload %s", upload.id)
    return removed
