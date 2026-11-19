from __future__ import annotations

from django.urls import path

from apps.manual.views import ManualDownloadView, ManualView

app_name = "manual"

urlpatterns = [
    path("", ManualView.as_view(), name="index"),
    path("descargar/", ManualDownloadView.as_view(), name="download"),
]
