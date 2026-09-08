from __future__ import annotations

from django.urls import path

from apps.core.views import HelpCenterView, PrivacyPolicyView

app_name = "core"

urlpatterns = [
    path("privacidad/", PrivacyPolicyView.as_view(), name="privacy"),
    path("ayuda/", HelpCenterView.as_view(), name="help"),
]
