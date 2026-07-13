from __future__ import annotations

from django.urls import path

from apps.analysis.views import DocumentAnalyzeView

app_name = "analysis"

urlpatterns = [
    path(
        "documentos/<uuid:pk>/analizar/",
        DocumentAnalyzeView.as_view(),
        name="analyze",
    ),
]