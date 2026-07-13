from __future__ import annotations

from django.urls import path

from apps.certificates.views import (
    CertificateDownloadView,
    CertificateGenerateView,
    CertificateVerifyView,
)

app_name = "certificates"

urlpatterns = [
    path(
        "documentos/<uuid:pk>/certificado/generar/",
        CertificateGenerateView.as_view(),
        name="generate",
    ),
    path(
        "certificados/<uuid:pk>/descargar/",
        CertificateDownloadView.as_view(),
        name="download",
    ),
    path(
        "certificados/verificar/<str:verification_hash>/",
        CertificateVerifyView.as_view(),
        name="verify",
    ),
]