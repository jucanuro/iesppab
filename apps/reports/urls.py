from __future__ import annotations

from django.urls import path

from apps.reports.views import DownloadHighlightedDocumentView, ReportDetailView

app_name = "reports"

urlpatterns = [
    path(
        "documentos/<uuid:pk>/reporte/",
        ReportDetailView.as_view(),
        name="detail",
    ),
    path(
        "documentos/<uuid:pk>/reporte/descargar-senalado/",
        DownloadHighlightedDocumentView.as_view(),
        name="download_highlighted",
    ),
]
