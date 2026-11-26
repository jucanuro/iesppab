from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from apps.documents.services import pending_authorizations_count


def pending_authorizations(request: HttpRequest) -> dict[str, Any]:
    """Número de envíos que esperan la decisión del usuario (pestaña del menú)."""

    user = getattr(request, "user", None)

    if user is None or not user.is_authenticated:
        return {"pending_authorizations_count": 0}

    return {"pending_authorizations_count": pending_authorizations_count(user)}
