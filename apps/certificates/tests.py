from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.template.defaultfilters import floatformat
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, UserRole
from apps.certificates.models import Certificate
from apps.certificates.services import CertificateGenerationService
from apps.core.models import Institution
from apps.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentText,
)
from apps.reports.models import (
    AI_RESPONSIBLE_USE_NOTE,
    AnalysisReport,
    ReportRiskLevel,
)

SAMPLE_TEXT = "Contenido academico de prueba para el certificado."


class CertificateRiskGateTests(TestCase):
    """El certificado no se emite para reportes con riesgo alto."""

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba",
        )
        self.student = User.objects.create_user(
            username="alumno1",
            email="alumno1@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

        self.document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis de prueba",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(SAMPLE_TEXT),
            sha256_hash="hash-tesis-prueba",
            status=DocumentStatus.COMPLETED,
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(SAMPLE_TEXT.encode("utf-8")),
            save=False,
        )
        self.document.save()

        DocumentText.objects.create(
            document=self.document,
            content=SAMPLE_TEXT,
            word_count=len(SAMPLE_TEXT.split()),
            character_count=len(SAMPLE_TEXT),
        )

        self.report = AnalysisReport.objects.create(
            document=self.document,
            similarity_percent=Decimal("10.00"),
            web_similarity_percent=Decimal("10.00"),
            internal_similarity_percent=Decimal("0.00"),
            ai_probability_percent=Decimal("15.00"),
            risk_level=ReportRiskLevel.LOW,
        )

        self.service = CertificateGenerationService(
            issued_by=self.student,
            absolute_base_url="http://testserver/",
        )

    def test_high_risk_report_cannot_be_certified(self) -> None:
        self.report.risk_level = ReportRiskLevel.HIGH
        self.report.save(update_fields=["risk_level"])

        with self.assertRaises(ValidationError) as exc:
            self.service.execute(document_id=self.document.id)

        self.assertIn("riesgo", exc.exception.messages[0].lower())
        self.assertFalse(
            Certificate.objects.filter(report=self.report).exists()
        )

    def test_critical_risk_report_cannot_be_certified(self) -> None:
        self.report.risk_level = ReportRiskLevel.CRITICAL
        self.report.save(update_fields=["risk_level"])

        with self.assertRaises(ValidationError) as exc:
            self.service.execute(document_id=self.document.id)

        self.assertIn("crítico", exc.exception.messages[0].lower())
        self.assertFalse(
            Certificate.objects.filter(report=self.report).exists()
        )

    def test_low_risk_report_is_certified(self) -> None:
        certificate = self.service.execute(document_id=self.document.id)

        self.assertTrue(certificate.pdf_file)
        self.assertEqual(certificate.report_id, self.report.id)


class CertificateVerifyViewTests(TestCase):
    """Verificación pública por QR (REQ-CER-01, REQ-CER-02)."""

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-verify",
        )
        self.student = User.objects.create_user(
            username="alumno-verify",
            email="alumno-verify@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )
        self.document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis verificable",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(SAMPLE_TEXT),
            sha256_hash="hash-tesis-verify",
            status=DocumentStatus.COMPLETED,
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(SAMPLE_TEXT.encode("utf-8")),
            save=False,
        )
        self.document.save()
        self.report = AnalysisReport.objects.create(
            document=self.document,
            similarity_percent=Decimal("12.00"),
            web_similarity_percent=Decimal("12.00"),
            internal_similarity_percent=Decimal("0.00"),
            ai_probability_percent=Decimal("35.00"),
            risk_level=ReportRiskLevel.LOW,
            engine_version="motor-test-1",
        )
        self.certificate = Certificate.objects.create(
            report=self.report,
            code="CER-TEST-0001",
            verification_hash="a" * 64,
            issued_by=self.student,
        )

    def _url(self, verification_hash: str) -> str:
        return reverse("certificates:verify", args=[verification_hash])

    def test_valid_certificate_shows_code_ai_estimate_and_note(self) -> None:
        response = self.client.get(self._url(self.certificate.verification_hash))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Certificado válido")
        self.assertContains(response, "CER-TEST-0001")
        self.assertContains(response, "motor-test-1")
        self.assertContains(response, "IA estimada")
        self.assertContains(response, "Indicios leves")  # banda 20-49
        self.assertContains(response, floatformat(Decimal("35.00"), 1))
        self.assertContains(response, floatformat(Decimal("12.00"), 0))
        self.assertContains(response, AI_RESPONSIBLE_USE_NOTE)
        self.assertEqual(
            response.context["ai_estimate"].engine_version, "motor-test-1"
        )

    def test_unknown_hash_is_not_valid_with_200(self) -> None:
        response = self.client.get(self._url("f" * 64))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no válido")
        self.assertNotContains(response, "Certificado válido")

    def test_inactive_certificate_is_not_valid_with_200(self) -> None:
        self.certificate.is_active = False
        self.certificate.revoked_at = timezone.now()
        self.certificate.save(update_fields=["is_active", "revoked_at"])

        response = self.client.get(self._url(self.certificate.verification_hash))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no válido")

    def test_broken_relation_does_not_cause_500(self) -> None:
        from unittest import mock

        with mock.patch(
            "apps.certificates.views.build_ai_estimate",
            side_effect=AttributeError("relación rota"),
        ):
            with self.assertLogs("apps.certificates.views", level="ERROR"):
                response = self.client.get(
                    self._url(self.certificate.verification_hash)
                )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no válido")
