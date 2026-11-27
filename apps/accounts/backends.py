from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q


class UsernameOrStaffEmailBackend(ModelBackend):
    """
    Igual que ModelBackend, pero si lo escrito no es un usuario y contiene
    «@», busca por correo (sin distinguir mayúsculas) **solo entre cuentas de
    staff o superusuario**. Así el login de /admin/ acepta usuario o correo
    institucional sin abrir el correo como credencial para el resto.

    No cambia el login institucional (InstitutionalLoginView), que resuelve
    el usuario por su cuenta y llama a authenticate() con el username. El
    panel /admin/ sigue exigiendo is_staff (AdminAuthenticationForm).
    """

    def authenticate(
        self,
        request: Any,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ):
        user = super().authenticate(
            request,
            username=username,
            password=password,
            **kwargs,
        )

        if user is not None or not username or "@" not in username or password is None:
            return user

        UserModel = get_user_model()
        candidate = (
            UserModel._default_manager.filter(email__iexact=username.strip())
            .filter(Q(is_staff=True) | Q(is_superuser=True))
            .first()
        )

        if candidate is None:
            # Igual que ModelBackend: iguala el tiempo de respuesta.
            UserModel().set_password(password)
            return None

        if candidate.check_password(password) and self.user_can_authenticate(candidate):
            return candidate

        return None
