from __future__ import annotations

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from .models import UserRole

logger = logging.getLogger(__name__)

User = get_user_model()


class InstitutionalLoginView(View):
    """
    Login institucional privado.
    Permite ingresar con usuario o correo.
    """

    template_name = "accounts/login.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("documents:upload")

        return render(
            request,
            self.template_name,
            {
                "next": request.GET.get("next", ""),
                "institution_name": 'IESPP "Alfonso Barrantes Lingán"',
                "institution_location": "San Miguel",
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        username_or_email = request.POST.get("username_or_email", "").strip()
        password = request.POST.get("password", "")
        next_url = request.POST.get("next", "")

        if not username_or_email or not password:
            messages.error(request, "Ingresa tu usuario/correo y contraseña.")
            return redirect("accounts:login")

        try:
            is_email_input = "@" in username_or_email

            if is_email_input:
                user_record = (
                    User.objects.select_related("institution")
                    .filter(email__iexact=username_or_email)
                    .first()
                )
            else:
                user_record = (
                    User.objects.select_related("institution")
                    .filter(username__iexact=username_or_email)
                    .first()
                )

            if user_record is None:
                messages.error(request, "Credenciales incorrectas.")
                return redirect("accounts:login")

            is_privileged_user = (
                user_record.role == UserRole.ADMIN or user_record.is_superuser
            )

            if not is_privileged_user:
                if not is_email_input:
                    messages.error(
                        request,
                        "Los usuarios institucionales deben ingresar con su "
                        "correo institucional, no con su usuario.",
                    )
                    return redirect("accounts:login")

                if user_record.institution_id and not (
                    user_record.institution.email_belongs_to_domain(
                        user_record.email
                    )
                ):
                    messages.error(request, "Credenciales incorrectas.")
                    return redirect("accounts:login")

            user = authenticate(
                request,
                username=user_record.username,
                password=password,
            )

            if user is None:
                messages.error(request, "Credenciales incorrectas.")
                return redirect("accounts:login")

            if not user.is_active:
                messages.error(request, "Tu cuenta se encuentra desactivada.")
                return redirect("accounts:login")

            login(request, user)

            logger.info("Login correcto. user_id=%s", user.id)

            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)

            return redirect("documents:upload")

        except Exception:
            logger.exception("Error inesperado en login institucional.")
            messages.error(request, "Ocurrió un error al iniciar sesión.")
            return redirect("accounts:login")


class InstitutionalLogoutView(LoginRequiredMixin, View):
    """
    Cierre de sesión seguro mediante POST.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        logout(request)
        messages.success(request, "Sesión cerrada correctamente.")
        return redirect("accounts:login")