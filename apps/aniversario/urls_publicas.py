"""URLconf del host del aniversario. Aquí no se incluye nada de originalidad."""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

urlpatterns = [
    path("", include("apps.aniversario.urls", namespace="aniversario")),
]

if settings.DEBUG:
    # Solo las fotos del concurso: nunca exponer media/ completo (tesis privadas).
    urlpatterns += static(
        f"{settings.MEDIA_URL}aniversario/",
        document_root=settings.MEDIA_ROOT / "aniversario",
    )

handler404 = "apps.aniversario.views.no_encontrado"
handler500 = "apps.aniversario.views.error_servidor"
