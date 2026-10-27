"""JSON endpoints for the web client, the file download and the single-page app."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Upload
from .storage import PART_SIZE, content_disposition, get_storage


def upload_json(upload: Upload) -> dict:
    return {
        "id": str(upload.id),
        "filename": upload.filename,
        "length": upload.length,
        "offset": upload.offset,
        "completed": upload.completed,
        "createdAt": upload.created_at.isoformat(),
        "expiresAt": upload.expires_at.isoformat(),
        "downloadUrl": f"/api/uploads/{upload.id}/download" if upload.completed else None,
    }


@require_GET
def config(request):
    used = Upload.objects.filter(created_at__gte=timezone.now() - timedelta(hours=24)).aggregate(total=Sum("length"))["total"] or 0
    return JsonResponse({
        "endpoint": "/files/",
        "maxUploadSize": settings.MAX_UPLOAD_SIZE,
        "chunkSize": PART_SIZE,  # one S3 part per request
        "ttlHours": settings.UPLOAD_TTL_HOURS,
        "storage": settings.UPLOAD_STORAGE,
        "dailyQuotaRemaining": max(0, settings.DAILY_BYTE_QUOTA - used),
    })


@require_GET
def upload_detail(request, upload_id):
    upload = Upload.objects.filter(pk=upload_id).first()
    if upload is None or upload.expired:
        raise Http404
    return JsonResponse(upload_json(upload))


@require_GET
def download(request, upload_id):
    upload = Upload.objects.filter(pk=upload_id).first()
    if upload is None or upload.expired or not upload.completed:
        raise Http404
    storage = get_storage()
    url = storage.download_url(upload)
    if url:
        return HttpResponseRedirect(url)  # short-lived presigned S3 URL
    resp = FileResponse(storage.open(upload), content_type="application/octet-stream")
    resp["Content-Disposition"] = content_disposition(upload.filename)
    return resp


@require_GET
def healthz(request):
    return HttpResponse("ok\n", content_type="text/plain")


@require_GET
def spa(request):
    try:
        return HttpResponse(get_template("index.html").render())
    except TemplateDoesNotExist:
        return HttpResponse("Frontend not built. Run `npm run build` in frontend/.", content_type="text/plain")
