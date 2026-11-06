from __future__ import annotations

import logging
from typing import Any, cast

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import TemplateView

from apps.accounts.models import User, UserRole
from apps.documents.exceptions import DocumentUploadError
from apps.documents.models import Document, DocumentKind, DocumentStatus
from apps.documents.services import DocumentUploadDTO, DocumentUploadService

DOCUMENTS_PER_PAGE = 10
DOCUMENTS_PER_PAGE_CHOICES = (10, 25, 50, 100)

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

        scoped_documents = self._scoped_documents(user)
        filtered_documents = self._apply_filters(scoped_documents)

        page_size = self._page_size()
        paginator = Paginator(filtered_documents, page_size)
        page = paginator.get_page(self.request.GET.get("page"))

        active_filters = {
            "estado": self.request.GET.get("estado", "").strip(),
            "tipo": self.request.GET.get("tipo", "").strip(),
            "q": self.request.GET.get("q", "").strip(),
        }

        context["document_kinds"] = DocumentKind.choices
        context["students"] = self._get_available_students(user)
        context["advisors"] = self._get_available_advisors(user)

        context["recent_documents"] = page
        context["page_obj"] = page
        context["paginator"] = paginator
        context["documents_total"] = paginator.count
        context["has_any_documents"] = scoped_documents.exists()

        context["status_choices"] = DocumentStatus.choices
        context["kind_choices"] = DocumentKind.choices
        context["active_filters"] = active_filters
        context["has_active_filters"] = any(active_filters.values())
        context["filter_querystring"] = self._filter_querystring()

        context["page_size"] = page_size
        context["page_size_choices"] = DOCUMENTS_PER_PAGE_CHOICES

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
                advisor_id=request.POST.get("advisor_id") or None,
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

    def _get_available_students(self, user: User) -> list[User]:
        if user.is_student_role:
            return []

        students = User.objects.select_related("institution").filter(
            role=UserRole.STUDENT,
            is_active=True,
        )

        if not user.is_superuser:
            students = students.filter(institution_id=user.institution_id)

        return [user, *students.order_by("first_name", "last_name", "username")]

    def _get_available_advisors(self, user: User) -> list[User]:
        advisors = User.objects.select_related("institution").filter(
            role__in=[UserRole.TEACHER, UserRole.DIRECTOR],
            is_active=True,
        )

        if not user.is_superuser:
            advisors = advisors.filter(institution_id=user.institution_id)

        return list(advisors.order_by("first_name", "last_name", "username"))

    def _scoped_documents(self, user: User):
        """Documentos visibles para el usuario, sin filtros de la UI."""
        documents = Document.objects.select_related(
            "institution",
            "owner",
            "uploaded_by",
            "report",
            "report__certificate",
        )

        if user.is_superuser or user.is_admin_role:
            if not user.is_superuser:
                documents = documents.filter(institution=user.institution)
        else:
            documents = documents.filter(Q(owner=user) | Q(uploaded_by=user))

        return documents.order_by("-created_at")

    def _apply_filters(self, documents):
        """Aplica los filtros de la bandeja (estado, tipo, búsqueda)."""
        estado = self.request.GET.get("estado", "").strip()
        if estado in DocumentStatus.values:
            documents = documents.filter(status=estado)

        tipo = self.request.GET.get("tipo", "").strip()
        if tipo in DocumentKind.values:
            documents = documents.filter(kind=tipo)

        query = self.request.GET.get("q", "").strip()
        if query:
            documents = documents.filter(
                Q(title__icontains=query)
                | Q(owner__first_name__icontains=query)
                | Q(owner__last_name__icontains=query)
                | Q(owner__username__icontains=query)
                | Q(owner__email__icontains=query)
            )

        return documents

    def _page_size(self) -> int:
        """Documentos por página (`?por_pagina=`), 10 por defecto."""
        raw_value = self.request.GET.get("por_pagina", "").strip()

        try:
            value = int(raw_value)
        except ValueError:
            return DOCUMENTS_PER_PAGE

        return value if value in DOCUMENTS_PER_PAGE_CHOICES else DOCUMENTS_PER_PAGE

    def _filter_querystring(self) -> str:
        """Querystring de los filtros activos (sin `page`), con `&` inicial."""
        params = self.request.GET.copy()
        params.pop("page", None)
        encoded = params.urlencode()
        return f"&{encoded}" if encoded else ""

    def _format_validation_error(self, error: ValidationError) -> str:
        if hasattr(error, "message_dict"):
            first_errors = next(iter(error.message_dict.values()), [])
            if first_errors:
                return str(first_errors[0])

        if hasattr(error, "messages") and error.messages:
            return str(error.messages[0])

        return "Los datos enviados no son válidos."