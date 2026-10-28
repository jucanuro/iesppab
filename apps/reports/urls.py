from __future__ import annotations

from django.urls import path

from apps.reports.views import (
    DownloadHighlightedDocumentView,
    ExcludeReportSourceView,
    ReportDetailView,
    ReportStatusView,
    RestoreReportSourceView,
)

app_name = "reports"

urlpatterns = [
    path(
        "documentos/<uuid:pk>/reporte/",
        ReportDetailView.as_view(),
        name="detail",
    ),
    path(
        "documentos/<uuid:pk>/reporte/estado/",
        ReportStatusView.as_view(),
        name="status",
    ),
    path(
        "documentos/<uuid:pk>/reporte/descargar-senalado/",
        DownloadHighlightedDocumentView.as_view(),
        name="download_highlighted",
    ),
    path(
        "documentos/<uuid:pk>/reporte/fuentes/<uuid:source_pk>/excluir/",
        ExcludeReportSourceView.as_view(),
        name="exclude_source",
    ),
    path(
        "documentos/<uuid:pk>/reporte/fuentes/<uuid:source_pk>/restaurar/",
        RestoreReportSourceView.as_view(),
        name="restore_source",
    ),
]
