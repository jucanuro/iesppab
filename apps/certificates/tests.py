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
    FindingType,
    ReportFinding,
    ReportRiskLevel,
    ReportSource,
    SourceType,
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


class CertificateVerifyConfidentialityTests(CertificateVerifyViewTests):
    """REQ-CER-08, RN-CER-04: la verificación pública no filtra contenido."""

    SECRET_TEXT = "CONTENIDO-PRIVADO-DEL-DOCUMENTO"

    def _source(self, title, source_type, metadata=None, **extra) -> ReportSource:
        return ReportSource.objects.create(
            report=self.report,
            source_type=source_type,
            title=title,
            url=extra.pop("url", ""),
            matched_percent=extra.pop("matched_percent", Decimal("5.00")),
            snippet=extra.pop("snippet", ""),
            metadata=metadata or {},
            **extra,
        )

    def _finding(self, source, start, end, word_count=None, **kwargs):
        metadata = {} if word_count is None else {"word_count": word_count}
        return ReportFinding.objects.create(
            report=self.report,
            source=source,
            finding_type=kwargs.pop("finding_type", FindingType.SIMILARITY),
            start_offset=start,
            end_offset=end,
            text_excerpt=kwargs.pop("text_excerpt", "extracto"),
            metadata=metadata,
            **kwargs,
        )

    def _get(self):
        response = self.client.get(self._url(self.certificate.verification_hash))
        self.assertEqual(response.status_code, 200)
        return response

    def test_internal_sources_are_aggregated_and_hidden_span(self) -> None:
        self.report.span_alignment = {"countable_word_count": 100}
        self.report.save(update_fields=["span_alignment"])
        DocumentText.objects.create(
            document=self.document,
            content=self.SECRET_TEXT,
            word_count=1,
            character_count=len(self.SECRET_TEXT),
        )
        internal_a = self._source(
            "TITULO-INTERNO-A",
            SourceType.INTERNAL,
            {"source_document_id": "id-interno-secreto-a"},
            url="https://interno.example/ruta-secreta-a",
            snippet="SNIPPET-SECRETO",
        )
        internal_b = self._source("TITULO-INTERNO-B", SourceType.INTERNAL)
        self._source(
            "TITULO-INTERNO-EXCLUIDO", SourceType.INTERNAL, is_excluded=True
        )
        self._source(
            "Página web visible",
            SourceType.WEB,
            url="https://web.example/visible",
        )
        self._source(
            "Tesis OAI visible",
            SourceType.INTERNAL,
            {"source": "oai", "repository_name": "Repositorio X"},
            url="https://oai.example/visible",
        )
        self._finding(internal_a, 0, 10, word_count=10)
        self._finding(internal_b, 20, 30, word_count=6)
        self._finding(
            None,
            40,
            50,
            finding_type=FindingType.AI_GENERATED,
            text_excerpt="TEXTO-IA-SECRETO",
        )

        response = self._get()

        self.assertContains(
            response,
            f"Base interna: 2 fuentes, {floatformat(Decimal('16.0'), 1)} %",
        )
        for hidden in (
            "TITULO-INTERNO-A",
            "TITULO-INTERNO-B",
            "TITULO-INTERNO-EXCLUIDO",
            "interno.example",
            "id-interno-secreto-a",
            "SNIPPET-SECRETO",
            "TEXTO-IA-SECRETO",
            self.SECRET_TEXT,
        ):
            self.assertNotContains(response, hidden)
        self.assertContains(response, "Página web visible")
        self.assertContains(response, "web.example/visible")
        self.assertContains(response, "Tesis OAI visible")
        self.assertContains(response, "oai.example/visible")
        self.assertContains(response, self.student.username)

    def test_internal_percent_is_union_not_sum_chunk_engine(self) -> None:
        words = [f"palabra{i}" for i in range(20)]
        content = " ".join(words)
        DocumentText.objects.create(
            document=self.document,
            content=content,
            word_count=20,
            character_count=len(content),
        )
        start_5 = content.index("palabra5 ")
        end_9 = content.index("palabra9") + len("palabra9")
        end_14 = content.index("palabra14") + len("palabra14")
        internal_a = self._source("Interna A", SourceType.INTERNAL)
        internal_b = self._source("Interna B", SourceType.INTERNAL)
        self._finding(internal_a, 0, end_9)  # palabras 0-9
        self._finding(internal_b, start_5, end_14)  # palabras 5-14, solapa

        response = self._get()

        # Unión = 15 de 20 palabras (75 %); la suma sería 100 %.
        self.assertContains(
            response,
            f"Base interna: 2 fuentes, {floatformat(Decimal('75.0'), 1)} %",
        )

    def test_report_without_internal_sources_has_no_internal_line(self) -> None:
        self._source("Página web visible", SourceType.WEB, url="https://w.example/x")

        response = self._get()

        self.assertNotContains(response, "Base interna")
        self.assertContains(response, "Página web visible")
