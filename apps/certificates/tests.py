from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase

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
from apps.reports.models import AnalysisReport, ReportRiskLevel

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

    def test_low_risk_report_is_certified(self) -> None:
        certificate = self.service.execute(document_id=self.document.id)

        self.assertTrue(certificate.pdf_file)
        self.assertEqual(certificate.report_id, self.report.id)
