from __future__ import annotations

from django.urls import path

from apps.reports.views import ReportDetailView

app_name = "reports"

urlpatterns = [
    path(
        "documentos/<uuid:pk>/reporte/",
        ReportDetailView.as_view(),
        name="detail",
    ),
]