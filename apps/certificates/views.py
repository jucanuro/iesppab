from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views import View

from apps.certificates.models import Certificate
from apps.certificates.services import (
    CertificateGenerationError,
    CertificateGenerationService,
)

logger = logging.getLogger(__name__)


class CertificateGenerateView(LoginRequiredMixin, View):
    """
    Genera el certificado PDF desde un reporte final.
    """

    def post(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        try:
            base_url = request.build_absolute_uri("/")
            service = CertificateGenerationService(
                issued_by=request.user,
                absolute_base_url=base_url,
            )

            certificate = service.execute(document_id=pk)

            messages.success(
                request,
                "Certificado generado correctamente.",
            )

            return redirect(
                "certificates:download",
                pk=certificate.id,
            )

        except PermissionDenied as exc:
            logger.warning(
                "Permiso denegado al generar certificado. user_id=%s document_id=%s",
                request.user.id,
                pk,
                exc_info=True,
            )
            return HttpResponse(str(exc), status=403)

        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect("reports:detail", pk=pk)

        except CertificateGenerationError as exc:
            messages.error(request, str(exc))
            return redirect("reports:detail", pk=pk)

        except Exception:
            logger.exception(
                "Error inesperado al generar certificado. user_id=%s document_id=%s",
                request.user.id,
                pk,
            )
            messages.error(
                request,
                "Ocurrió un error inesperado al generar el certificado.",
            )
            return redirect("reports:detail", pk=pk)


class CertificateDownloadView(LoginRequiredMixin, View):
    """
    Descarga segura del certificado PDF.
    """

    def get(
        self,
        request: HttpRequest,
        pk: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> FileResponse:
        certificate = self._get_allowed_certificate(
            certificate_id=pk,
            request=request,
        )

        if not certificate.pdf_file:
            raise Http404("El certificado aún no tiene archivo PDF.")

        return FileResponse(
            certificate.pdf_file.open("rb"),
            as_attachment=True,
            filename=f"{certificate.code}.pdf",
            content_type="application/pdf",
        )

    def _get_allowed_certificate(
        self,
        certificate_id: UUID,
        request: HttpRequest,
    ) -> Certificate:
        queryset = Certificate.objects.select_related(
            "report",
            "report__document",
            "report__document__institution",
            "report__document__owner",
        ).filter(
            id=certificate_id,
            is_active=True,
        )

        user = request.user

        if user.is_student_role:
            queryset = queryset.filter(report__document__owner=user)

        elif not user.is_superuser:
            if user.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            queryset = queryset.filter(
                report__document__institution=user.institution,
            )

        certificate = queryset.first()

        if certificate is None:
            raise Http404("Certificado no encontrado.")

        return certificate


class CertificateVerifyView(View):
    """
    Vista pública simple para validar autenticidad por hash.
    """

    template_name = "certificates/verify.html"

    def get(
        self,
        request: HttpRequest,
        verification_hash: str,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        certificate = (
            Certificate.objects.select_related(
                "report",
                "report__document",
                "report__document__institution",
                "report__document__owner",
            )
            .filter(
                verification_hash=verification_hash,
                is_active=True,
            )
            .first()
        )

        return render(
            request,
            self.template_name,
            {
                "certificate": certificate,
                "verification_hash": verification_hash,
            },
        )