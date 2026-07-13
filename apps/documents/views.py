from __future__ import annotations

import logging
from typing import Any, cast

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from apps.accounts.models import User, UserRole
from apps.documents.exceptions import DocumentUploadError
from apps.documents.models import Document, DocumentKind
from apps.documents.services import DocumentUploadDTO, DocumentUploadService

logger = logging.getLogger(__name__)


class DocumentUploadView(LoginRequiredMixin, TemplateView):
    """
    Dashboard privado de documentos.

    GET:
        Muestra bandeja de documentos y panel de carga.

    POST:
        Procesa carga segura sin forms.py.
    """

    template_name = "documents/upload.html"
    login_url = reverse_lazy("accounts:login")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = cast(User, self.request.user)

        context["document_kinds"] = DocumentKind.choices
        context["students"] = self._get_available_students(user)
        context["recent_documents"] = self._get_recent_documents(user)

        return context

    def post(
        self,
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        user = cast(User, request.user)

        try:
            uploaded_file = request.FILES.get("document_file")

            if uploaded_file is None:
                messages.error(request, "Debes seleccionar un archivo PDF o DOCX.")
                return redirect("documents:upload")

            dto = DocumentUploadDTO(
                title=request.POST.get("title", ""),
                kind=request.POST.get("kind", ""),
                course_name=request.POST.get("course_name", ""),
                academic_period=request.POST.get("academic_period", ""),
                owner_id=request.POST.get("owner_id") or None,
                uploaded_file=uploaded_file,
            )

            service = DocumentUploadService(uploaded_by=user)
            document = service.execute(dto)

            messages.success(
                request,
                f"El documento '{document.title}' fue enviado correctamente.",
            )

            return redirect("documents:upload")

        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al cargar documento. user_id=%s",
                user.id,
                exc_info=True,
            )
            return HttpResponseForbidden(str(exc))

        except ValidationError as exc:
            logger.warning(
                "Validación fallida al cargar documento. user_id=%s",
                user.id,
                exc_info=True,
            )
            messages.error(request, self._format_validation_error(exc))
            return redirect("documents:upload")

        except DocumentUploadError as exc:
            logger.warning(
                "Error de carga de documento. user_id=%s",
                user.id,
                exc_info=True,
            )
            messages.error(request, str(exc))
            return redirect("documents:upload")

        except Exception:
            logger.exception(
                "Error inesperado en DocumentUploadView. user_id=%s",
                user.id,
            )
            messages.error(
                request,
                "Ocurrió un error inesperado. Intenta nuevamente.",
            )
            return redirect("documents:upload")

    def _get_available_students(self, user: User):
        if user.is_student_role:
            return User.objects.none()

        students = User.objects.select_related("institution").filter(
            role=UserRole.STUDENT,
            is_active=True,
        )

        if not user.is_superuser:
            students = students.filter(institution_id=user.institution_id)

        return students.order_by("first_name", "last_name", "username")

    def _get_recent_documents(self, user: User):
        documents = Document.objects.select_related(
            "institution",
            "owner",
            "uploaded_by",
            "report",
        )

        if user.is_student_role:
            documents = documents.filter(owner=user)
        elif not user.is_superuser:
            documents = documents.filter(institution=user.institution)

        return documents.order_by("-created_at")[:10]

    def _format_validation_error(self, error: ValidationError) -> str:
        if hasattr(error, "message_dict"):
            first_errors = next(iter(error.message_dict.values()), [])
            if first_errors:
                return str(first_errors[0])

        if hasattr(error, "messages") and error.messages:
            return str(error.messages[0])

        return "Los datos enviados no son válidos."