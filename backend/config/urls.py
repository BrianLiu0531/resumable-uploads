from django.urls import path

from uploads import views
from uploads.tus import TusView

urlpatterns = [
    path("files/", TusView.as_view(), name="tus-create"),
    path("files/<uuid:upload_id>", TusView.as_view(), name="tus-upload"),
    path("api/config", views.config),
    path("api/uploads/<uuid:upload_id>", views.upload_detail),
    path("api/uploads/<uuid:upload_id>/download", views.download),
    path("healthz", views.healthz),
    path("", views.spa),
]
