from __future__ import annotations

from django.urls import path

from apps.documents.views import (
    AuthorizationDecisionView,
    AuthorizationInboxView,
    DocumentUploadView,
    MarkAlumniView,
)

app_name = "documents"

urlpatterns = [
    path("documentos/subir/", DocumentUploadView.as_view(), name="upload"),
    path(
        "documentos/solicitudes/",
        AuthorizationInboxView.as_view(),
        name="authorizations",
    ),
    path(
        "documentos/<uuid:pk>/autorizacion/",
        AuthorizationDecisionView.as_view(),
        name="authorization-decision",
    ),
    path(
        "alumnos/<uuid:pk>/egresado/",
        MarkAlumniView.as_view(),
        name="mark-alumni",
    ),
]
