from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    InstitutionalLoginView,
    InstitutionalLogoutView,
    PublicRegistrationView,
)

app_name = "accounts"

urlpatterns = [
    path("", InstitutionalLoginView.as_view(), name="login"),
    path("salir/", InstitutionalLogoutView.as_view(), name="logout"),
    path("registro/", PublicRegistrationView.as_view(), name="register"),
]