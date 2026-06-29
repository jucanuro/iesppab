from __future__ import annotations

from django.urls import path

from apps.documents.views import DocumentUploadView

app_name = "documents"

urlpatterns = [
    path("documentos/subir/", DocumentUploadView.as_view(), name="upload"),
]