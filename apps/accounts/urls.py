from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    BulkEnableStudentsView,
    InstitutionalLoginView,
    InstitutionalLogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
    PendingStudentsView,
    PublicRegistrationView,
)

app_name = "accounts"

# Recuperación de contraseña: subclases de las vistas integradas de Django
# (ver apps/accounts/views.py) con plantillas propias en
# templates/accounts/password_reset_*.
password_reset_urlpatterns = [
    path(
        "clave/recuperar/",
        PasswordResetView.as_view(),
        name="password_reset",
    ),
    path(
        "clave/recuperar/enviado/",
        PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "clave/nueva/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "clave/nueva/lista/",
        PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
]

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
    *password_reset_urlpatterns,
]
