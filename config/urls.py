from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path,include

from apps.accounts.admin_forms import InstitutionalAdminAuthenticationForm


admin.site.site_header = 'IESPP "Alfonso Barrantes Lingán" · Administración'
admin.site.site_title = "Administración IESPP ABL"
admin.site.index_title = "Panel de administración"
# Usuario o correo institucional (ver apps/accounts/backends.py).
admin.site.login_form = InstitutionalAdminAuthenticationForm


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.core.urls")),
    path("", include("apps.accounts.urls")),
    path("", include("apps.documents.urls", namespace="documents")),
    path("", include("apps.analysis.urls")),
    path("", include("apps.reports.urls")),
    path("", include("apps.certificates.urls")),
    path("manual/", include("apps.manual.urls")),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )