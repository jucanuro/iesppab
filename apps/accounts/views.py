from __future__ import annotations

import logging
import re
from typing import Any

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView

from apps.core.models import Institution

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


class PublicRegistrationView(View):
    """
    Auto-registro público para alumnos con correo institucional.
    """

    template_name = "accounts/register.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            return redirect("documents:upload")

        institution = Institution.objects.filter(is_active=True).first()

        return render(
            request,
            self.template_name,
            {
                "institution_name": (
                    institution.name
                    if institution
                    else 'IESPP "Alfonso Barrantes Lingán"'
                ),
                "institution_location": "San Miguel",
                "email_domain": institution.email_domain if institution else "",
            },
        )

    def post(self, request: HttpRequest) -> HttpResponse:
        email = request.POST.get("email", "").strip().lower()
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")

        if not email or not first_name or not last_name or not password1 or not password2:
            messages.error(request, "Completa todos los campos para registrarte.")
            return redirect("accounts:register")

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Ingresa un correo electrónico válido.")
            return redirect("accounts:register")

        if password1 != password2:
            messages.error(request, "Las contraseñas no coinciden.")
            return redirect("accounts:register")

        try:
            validate_password(password1)
        except ValidationError as exc:
            for error_message in exc.messages:
                messages.error(request, error_message)
            return redirect("accounts:register")

        try:
            institution = Institution.objects.filter(is_active=True).first()

            if institution is None or not institution.email_domain:
                messages.error(
                    request,
                    "El registro público no está disponible en este momento.",
                )
                return redirect("accounts:register")

            if not institution.email_belongs_to_domain(email):
                messages.error(
                    request,
                    "El correo debe pertenecer al dominio institucional "
                    f"@{institution.email_domain}.",
                )
                return redirect("accounts:register")

            if User.objects.filter(email__iexact=email).exists():
                messages.error(
                    request,
                    "Ya existe una cuenta registrada con este correo.",
                )
                return redirect("accounts:register")

            username = self._generate_unique_username(email=email)

            user = User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=UserRole.STUDENT,
                institution=institution,
                is_active=True,
                is_enabled=False,
            )
            user.set_password(password1)
            user.save()

            logger.info("Registro público correcto. user_id=%s", user.id)

            messages.success(
                request,
                "Cuenta creada. Un docente debe habilitarte antes de que "
                "puedas subir documentos.",
            )
            return redirect("accounts:login")

        except Exception:
            logger.exception("Error inesperado en registro público.")
            messages.error(request, "Ocurrió un error al crear tu cuenta.")
            return redirect("accounts:register")

    def _generate_unique_username(self, email: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9._-]", "", email.split("@")[0]).lower()
        base = base or "usuario"
        username = base
        suffix = 1

        while User.objects.filter(username__iexact=username).exists():
            suffix += 1
            username = f"{base}{suffix}"

        return username


def _ensure_can_manage_pending_students(user: Any) -> None:
    can_manage = (
        user.is_superuser
        or user.is_teacher_role
        or user.is_director_role
        or user.is_admin_role
    )

    if not can_manage:
        raise PermissionDenied(
            "No tienes permisos para habilitar alumnos."
        )


class PendingStudentsView(LoginRequiredMixin, TemplateView):
    """
    Pantalla de habilitación en bloque de alumnos auto-registrados
    (role=STUDENT, is_enabled=False) mediante selección tipo "tags".
    """

    template_name = "accounts/pending_students.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            _ensure_can_manage_pending_students(request.user)
        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al ver alumnos pendientes. user_id=%s",
                request.user.id,
                exc_info=True,
            )
            return HttpResponseForbidden(str(exc))

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user

        pending_students = User.objects.filter(
            role=UserRole.STUDENT,
            is_enabled=False,
        )

        if not user.is_superuser:
            pending_students = pending_students.filter(
                institution_id=user.institution_id
            )

        pending_students = pending_students.order_by(
            "first_name", "last_name", "username"
        )

        pending_students_data = [
            {
                "id": str(student.id),
                "name": student.get_full_name().strip() or student.username,
                "email": student.email,
            }
            for student in pending_students
        ]

        context["pending_students"] = pending_students
        context["pending_students_data"] = pending_students_data

        return context


class BulkEnableStudentsView(LoginRequiredMixin, View):
    """
    Habilita en bloque a los alumnos seleccionados en la pantalla de
    pendientes.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = request.user

        try:
            _ensure_can_manage_pending_students(user)
        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al habilitar alumnos. user_id=%s",
                user.id,
                exc_info=True,
            )
            return HttpResponseForbidden(str(exc))

        student_ids = request.POST.getlist("student_ids")

        if not student_ids:
            messages.error(request, "Selecciona al menos un alumno para habilitar.")
            return redirect("accounts:pending-students")

        students = User.objects.filter(
            id__in=student_ids,
            role=UserRole.STUDENT,
            is_enabled=False,
        )

        if not user.is_superuser:
            students = students.filter(institution_id=user.institution_id)

        enabled_count = students.update(is_enabled=True)

        if enabled_count:
            messages.success(
                request,
                f"{enabled_count} alumno(s) habilitado(s) correctamente.",
            )
        else:
            messages.error(
                request,
                "No se pudo habilitar a los alumnos seleccionados.",
            )

        return redirect("accounts:pending-students")