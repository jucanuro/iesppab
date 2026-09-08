from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from apps.core.models import Institution


def institution_status(request: HttpRequest) -> dict[str, Any]:
    """
    Expone a las plantillas si la plataforma tiene una institución activa.
    Cuando no la hay, la mayoría de flujos de usuario no funcionan, así que
    `base.html` muestra un aviso de configuración pendiente.
    """
    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {"no_active_institution": False}

    return {
        "no_active_institution": not Institution.objects.filter(
            is_active=True,
        ).exists(),
    }
