from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    BulkEnableStudentsView,
    InstitutionalLoginView,
    InstitutionalLogoutView,
    PendingStudentsView,
    PublicRegistrationView,
)

app_name = "accounts"

urlpatterns = [
    path("", InstitutionalLoginView.as_view(), name="login"),
    path("salir/", InstitutionalLogoutView.as_view(), name="logout"),
    path("registro/", PublicRegistrationView.as_view(), name="register"),
    path(
        "accounts/pendientes/",
        PendingStudentsView.as_view(),
        name="pending-students",
    ),
    path(
        "accounts/pendientes/habilitar/",
        BulkEnableStudentsView.as_view(),
        name="bulk-enable-students",
    ),
]