from __future__ import annotations

import random
from decimal import Decimal
from io import BytesIO, StringIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, UserRole
from apps.analysis.engines.span_similarity import (
    SOURCE_KIND_OAI,
    SpanAnalysisResult,
    SpanSource,
    SpanSourceResult,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import WebPageContent
from apps.analysis.indexers import DocumentKnowledgeIndexer
from apps.analysis.models import OaiFullText, OaiFullTextStatus, OaiRecord
from apps.analysis.services import DocumentAnalysisService
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
    AI_CLASSIFIER_NOTE,
    AI_ESTIMATE_DISCLAIMER,
    AI_RESPONSIBLE_USE_NOTE,
    AnalysisReport,
    FindingType,
    ReportRiskLevel,
    ReportSource,
    SourceType,
)
from apps.reports.services import (
    AI_LIMITATIONS,
    ReportSourceExclusionError,
    ReportSourceExclusionService,
    SpanReportWriter,
    _HighlightedDocumentPdfBuilder,
    build_ai_report_pdf,
    build_highlighted_document_pdf,
    findings_in_paint_order,
    resolve_analyzed_content,
    resolve_risk_level,
    resubmission_warning_sources,
)

SAMPLE_TEXT = (
    "El presente trabajo analiza la metodologia aplicada en el estudio de "
    "los factores institucionales relacionados con el aprendizaje. Este "
    "parrafo contiene un fragmento que fue redactado probablemente por un "
    "modelo de lenguaje."
)


class ReportsTestCaseMixin:
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
        self.other_student = User.objects.create_user(
            username="alumno2",
            email="alumno2@example.com",
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
            similarity_percent=Decimal("35.00"),
            web_similarity_percent=Decimal("35.00"),
            internal_similarity_percent=Decimal("0.00"),
            ai_probability_percent=Decimal("40.00"),
            risk_level=ReportRiskLevel.HIGH,
        )

        self.source = ReportSource.objects.create(
            report=self.report,
            source_type=SourceType.WEB,
            title="Metodologia de investigacion",
            url="https://example.com/articulo",
            domain="example.com",
            matched_percent=Decimal("35.00"),
            snippet="fragmento coincidente",
        )

        similarity_start = SAMPLE_TEXT.index("metodologia aplicada")
        similarity_end = similarity_start + len("metodologia aplicada")

        self.similarity_finding = self.report.findings.create(
            source=self.source,
            finding_type=FindingType.SIMILARITY,
            start_offset=similarity_start,
            end_offset=similarity_end,
            text_excerpt=SAMPLE_TEXT[similarity_start:similarity_end],
            confidence_percent=Decimal("35.00"),
        )

        ai_start = SAMPLE_TEXT.index("modelo de lenguaje")
        ai_end = ai_start + len("modelo de lenguaje")

        self.ai_finding = self.report.findings.create(
            source=None,
            finding_type=FindingType.AI_GENERATED,
            start_offset=ai_start,
            end_offset=ai_end,
            text_excerpt=SAMPLE_TEXT[ai_start:ai_end],
            confidence_percent=Decimal("40.00"),
        )


class HighlightedDocumentPdfServiceTests(ReportsTestCaseMixin, TestCase):
    def test_generates_a_non_empty_pdf_with_similarity_and_ai_findings(self) -> None:
        pdf_bytes = build_highlighted_document_pdf(report=self.report)

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 1000)

    def test_pdf_has_the_originality_report_layout(self) -> None:
        from io import BytesIO

        from pypdf import PdfReader

        pdf_bytes = build_highlighted_document_pdf(report=self.report)
        text = "\n".join(
            page.extract_text() for page in PdfReader(BytesIO(pdf_bytes)).pages
        )

        self.assertIn("INFORME DE ORIGINALIDAD", text)
        self.assertIn("ÍNDICE DE SIMILITUD", text)
        self.assertIn("FUENTES DE INTERNET", text)
        self.assertIn("FUENTES PRIMARIAS", text)
        self.assertIn("TEXTO ANALIZADO", text)
        # el dominio de la fuente citada aparece en la lista de fuentes primarias
        self.assertIn("example.com", text)


class SimilarityPdfContentTests(ReportsTestCaseMixin, TestCase):
    """REQ-CER-03, REQ-CER-04, RN-CER-06, RN-CER-09."""

    def _text(self, report=None) -> str:
        from pypdf import PdfReader

        pdf = build_highlighted_document_pdf(report=report or self.report)
        return " ".join(
            " ".join(page.extract_text().split())
            for page in PdfReader(BytesIO(pdf)).pages
        )

    def test_has_institution_header_and_total_internal_web_percents(self) -> None:
        self.report.similarity_percent = Decimal("50.00")
        self.report.internal_similarity_percent = Decimal("12.50")
        self.report.web_similarity_percent = Decimal("41.30")
        self.report.save()

        text = self._text()

        self.assertIn(self.institution.name.upper(), text)
        self.assertIn("ÍNDICE DE SIMILITUD (TOTAL)", text)
        self.assertIn("TRABAJOS / REPOSITORIOS (INTERNA)", text)
        self.assertIn("FUENTES DE INTERNET (WEB)", text)
        self.assertIn("50,0", text)
        self.assertIn("12,5", text)
        self.assertIn("41,3", text)

    def test_sources_table_has_percent_column_and_type(self) -> None:
        text = self._text()

        self.assertIn("Coincidencia con la fuente", text)
        self.assertIn("no acumulable", text)
        self.assertIn("35,0%", text)
        self.assertIn("Web · example.com", text)

    def test_includes_calculation_note_exactly(self) -> None:
        from apps.reports.services import SIMILARITY_CALC_NOTE

        self.assertIn(" ".join(SIMILARITY_CALC_NOTE.split()), self._text())

    def test_does_not_paint_ai_findings(self) -> None:
        builder = _HighlightedDocumentPdfBuilder(report=self.report)

        markup, _, _ = builder._render_body_markup(
            content=SAMPLE_TEXT,
            findings=list(self.report.findings.all()),
            sources_by_id={self.source.id: self.source},
        )

        self.assertNotIn(builder.AI_BACKGROUND, markup)

    def test_overlapping_sources_print_total_not_sum(self) -> None:
        # Los % persistidos de dos fuentes que se solapan en el documento:
        # 40 % y 30 % (suma 70) con total 50 %. El PDF imprime lo persistido.
        self.report.similarity_percent = Decimal("50.00")
        self.report.save()
        self.source.matched_percent = Decimal("40.00")
        self.source.save()
        second_start = SAMPLE_TEXT.index("Este")
        second = ReportSource.objects.create(
            report=self.report,
            source_type=SourceType.WEB,
            title="Segunda fuente solapada",
            url="https://example.org/otra",
            domain="example.org",
            matched_percent=Decimal("30.00"),
        )
        self.report.findings.create(
            source=second,
            finding_type=FindingType.SIMILARITY,
            start_offset=second_start,
            end_offset=second_start + len("Este parrafo"),
            text_excerpt="Este parrafo",
            confidence_percent=Decimal("30.00"),
        )

        text = self._text()

        self.assertIn("50,0", text)
        self.assertIn("40,0%", text)
        self.assertIn("30,0%", text)
        self.assertNotIn("70,0", text)
        total = self.report.similarity_percent
        self.assertLess(total, Decimal("40.00") + Decimal("30.00"))

    def test_logs_warning_when_sources_sum_is_below_total(self) -> None:
        # Fixture: total 35 % con una fuente de 0,4 % (inconsistente).
        self.source.matched_percent = Decimal("0.40")
        self.source.save()

        with self.assertLogs("apps.reports.services", level="WARNING"):
            text = self._text()

        self.assertIn("ÍNDICE DE SIMILITUD", text)  # se imprime igual

    def test_download_highlighted_serves_the_similarity_pdf(self) -> None:
        from pypdf import PdfReader

        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:download_highlighted", kwargs={"pk": self.document.id}),
        )

        self.assertEqual(response.status_code, 200)
        text = " ".join(
            page.extract_text()
            for page in PdfReader(BytesIO(response.getvalue())).pages
        )
        self.assertIn("ÍNDICE DE SIMILITUD (TOTAL)", text)
        self.assertNotIn("IA estimada", text)

    def test_detail_button_is_labeled_similarity_report(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

        self.assertContains(response, "Informe de similitud")


class AiReportPdfTests(ReportsTestCaseMixin, TestCase):
    """REQ-CER-05, REQ-CER-06 (informe de IA estimada)."""

    def _text(self, pdf: bytes) -> str:
        from pypdf import PdfReader

        return " ".join(
            " ".join(page.extract_text().split())
            for page in PdfReader(BytesIO(pdf)).pages
        )

    def test_has_estimate_band_date_version_segments_method_limits_and_note(self) -> None:
        from django.template.defaultfilters import floatformat

        from apps.reports.ai_estimate import build_ai_estimate
        from apps.reports.models import AI_RESPONSIBLE_USE_NOTE

        self.report.engine_version = "motor-historico-0.9"
        self.report.save(update_fields=["engine_version"])
        estimate = build_ai_estimate(self.report)

        text = self._text(build_ai_report_pdf(self.report))

        self.assertIn("INFORME DE IA ESTIMADA", text)
        self.assertIn(f"IA estimada: {floatformat(estimate.percent, 1)}%", text)
        self.assertIn(estimate.band_label, text)
        self.assertIn("motor-historico-0.9", text)
        self.assertIn(
            timezone.localtime(estimate.generated_at).strftime("%d/%m/%Y %H:%M"),
            text,
        )
        # Probabilidad del segmento señalado (hallazgo AI_GENERATED de 40 %).
        self.assertIn("SEGMENTOS SEÑALADOS", text)
        self.assertIn("40,0%", text)
        self.assertIn("modelo de lenguaje", text)
        self.assertIn("MÉTODO", text)
        self.assertIn(estimate.method, text)
        self.assertIn("LIMITACIONES", text)
        for limitation in AI_LIMITATIONS:
            self.assertIn(" ".join(limitation.split()), text)
        self.assertIn(" ".join(AI_RESPONSIBLE_USE_NOTE.split()), text)

    def test_values_match_certificate_verification(self) -> None:
        from django.template.defaultfilters import floatformat

        from apps.reports.ai_estimate import build_ai_estimate

        estimate = build_ai_estimate(self.report)
        certificate = Certificate.objects.create(
            report=self.report,
            code="CERT-IA-1",
            verification_hash="c" * 64,
            issued_by=self.student,
        )

        text = self._text(build_ai_report_pdf(self.report))
        verify = self.client.get(
            reverse("certificates:verify", args=[certificate.verification_hash])
        )

        for value in (
            f"{floatformat(estimate.percent, 1)}%",
            estimate.band_label,
            estimate.version_label,
        ):
            self.assertIn(value, text)
            self.assertContains(verify, value)

    def test_report_keeps_its_own_engine_version(self) -> None:
        self.report.engine_version = "version-anterior-1.0"
        self.report.save(update_fields=["engine_version"])

        text = self._text(build_ai_report_pdf(self.report))

        self.assertIn("version-anterior-1.0", text)

    def test_without_findings_pdf_is_valid_with_empty_marker(self) -> None:
        self.report.findings.filter(finding_type=FindingType.AI_GENERATED).delete()

        pdf = build_ai_report_pdf(self.report)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn("Sin segmentos señalados", self._text(pdf))

    def test_download_view_permissions(self) -> None:
        url = reverse("reports:download_ai_report", kwargs={"pk": self.document.id})

        self.assertEqual(self.client.get(url).status_code, 302)

        self.client.force_login(self.other_student)
        self.assertEqual(self.client.get(url).status_code, 403)

        self.client.force_login(self.student)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("INFORME DE IA ESTIMADA", self._text(response.getvalue()))

    def test_detail_has_ai_report_button(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

        self.assertContains(response, "Informe de IA")
        self.assertContains(
            response,
            reverse("reports:download_ai_report", kwargs={"pk": self.document.id}),
        )


class UnfinishedReportDownloadTests(ReportsTestCaseMixin, TestCase):
    """REQ-CER-10: sin reporte terminado se muestra un mensaje, no un PDF."""

    URL_NAMES = ("reports:download_highlighted", "reports:download_ai_report")

    def _assert_redirects_with_message(self, url_name: str) -> None:
        from django.contrib.messages import get_messages

        self.client.force_login(self.student)
        detail_url = reverse("reports:detail", kwargs={"pk": self.document.id})

        response = self.client.get(
            reverse(url_name, kwargs={"pk": self.document.id})
        )

        self.assertRedirects(response, detail_url, fetch_redirect_response=False)
        self.assertNotEqual(response.get("Content-Type"), "application/pdf")
        # Los mensajes se acumulan entre subTests en la misma sesión: se
        # comprueba el último, que es el de esta petición.
        messages_text = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn("no ha terminado", messages_text[-1])

    def test_report_not_final_redirects_with_message(self) -> None:
        self.report.is_final = False
        self.report.save(update_fields=["is_final"])

        for url_name in self.URL_NAMES:
            with self.subTest(url_name=url_name):
                self._assert_redirects_with_message(url_name)

    def test_document_not_completed_redirects_with_message(self) -> None:
        for status in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING, DocumentStatus.FAILED):
            self.document.status = status
            self.document.save(update_fields=["status"])

            for url_name in self.URL_NAMES:
                with self.subTest(status=status, url_name=url_name):
                    self._assert_redirects_with_message(url_name)

    def test_document_without_report_redirects_with_message(self) -> None:
        self.report.delete()

        for url_name in self.URL_NAMES:
            with self.subTest(url_name=url_name):
                self._assert_redirects_with_message(url_name)

    def test_pdf_error_redirects_with_its_message(self) -> None:
        from django.contrib.messages import get_messages

        self.document.extracted_text.delete()
        self.report.analyzed_content = ""
        self.report.save(update_fields=["analyzed_content"])
        self.client.force_login(self.student)

        for url_name in self.URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(url_name, kwargs={"pk": self.document.id})
                )

                self.assertEqual(response.status_code, 302)
                self.assertIn("texto extraído", str(list(get_messages(response.wsgi_request))[-1]))

    def test_finished_report_still_downloads(self) -> None:
        self.client.force_login(self.student)

        for url_name in self.URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(url_name, kwargs={"pk": self.document.id})
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")


class PrintDesignTests(ReportsTestCaseMixin, TestCase):
    """REQ-CER-12, RN-CER-07: A4, paginación y rótulos de texto."""

    def setUp(self) -> None:
        super().setUp()
        # Texto largo para que el PDF ocupe varias páginas (los hallazgos
        # están dentro de los primeros caracteres, sus offsets siguen valiendo).
        self.report.analyzed_content = SAMPLE_TEXT + " " + (SAMPLE_TEXT + " ") * 150
        self.report.save(update_fields=["analyzed_content"])

    def _pdfs(self) -> dict[str, bytes]:
        return {
            "similitud": build_highlighted_document_pdf(report=self.report),
            "ia": build_ai_report_pdf(report=self.report),
        }

    def _reader(self, pdf: bytes):
        from pypdf import PdfReader

        return PdfReader(BytesIO(pdf))

    def test_pages_are_a4(self) -> None:
        for name, pdf in self._pdfs().items():
            with self.subTest(pdf=name):
                for page in self._reader(pdf).pages:
                    self.assertAlmostEqual(float(page.mediabox.width), 595.28, delta=1)
                    self.assertAlmostEqual(float(page.mediabox.height), 841.89, delta=1)

    def test_pages_are_numbered_x_of_n(self) -> None:
        for name, pdf in self._pdfs().items():
            with self.subTest(pdf=name):
                pages = self._reader(pdf).pages
                total = len(pages)

                self.assertGreater(total, 1)
                for number, page in enumerate(pages, start=1):
                    text = " ".join(page.extract_text().split())
                    self.assertIn(f"Página {number} de {total}", text)

    def test_text_labels_besides_color(self) -> None:
        import re

        sim_text = " ".join(
            page.extract_text()
            for page in self._reader(self._pdfs()["similitud"]).pages
        )
        ai_text = " ".join(
            " ".join(page.extract_text().split())
            for page in self._reader(self._pdfs()["ia"]).pages
        )

        self.assertIn("Coincidencia con fuente 1", " ".join(sim_text.split()))
        self.assertRegex(ai_text, r"IA 40,0 %")
        self.assertTrue(re.search(r"\[1 · IA 40,0 %\]", ai_text))

    def test_all_text_is_at_least_8pt(self) -> None:
        from apps.reports.services import MIN_PDF_FONT_SIZE

        for name, pdf in self._pdfs().items():
            sizes: set[float] = set()

            def visitor(text, cm, tm, font_dict, font_size, sizes=sizes):
                if text.strip():
                    # Tamaño efectivo: el del operador Tf por la escala de la matriz.
                    sizes.add(round(font_size * abs(tm[3]), 2))

            for page in self._reader(pdf).pages:
                page.extract_text(visitor_text=visitor)

            with self.subTest(pdf=name):
                self.assertTrue(sizes)
                self.assertGreaterEqual(min(sizes), MIN_PDF_FONT_SIZE, sorted(sizes))


class SimilarityInsideAiFindingTests(ReportsTestCaseMixin, TestCase):
    """
    Un pasaje de una fuente dentro de un párrafo marcado como IA suma al % y
    se pinta como similitud: la IA se parte alrededor. Antes el visor y el
    PDF se quedaban con el primer hallazgo (la IA) y la fuente salía en la
    lista sin ningún pasaje marcado.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ai_finding.start_offset = 0
        self.ai_finding.end_offset = len(SAMPLE_TEXT)
        self.ai_finding.save()
        self.source.matched_percent = Decimal("0.40")
        self.source.save()

    def test_paint_order_splits_ai_around_similarity(self) -> None:
        pieces = findings_in_paint_order(
            [self.ai_finding, self.similarity_finding], len(SAMPLE_TEXT)
        )
        start = self.similarity_finding.start_offset
        end = self.similarity_finding.end_offset

        self.assertEqual(
            [(a, b, finding.finding_type) for a, b, finding in pieces],
            [
                (0, start, FindingType.AI_GENERATED),
                (start, end, FindingType.SIMILARITY),
                (end, len(SAMPLE_TEXT), FindingType.AI_GENERATED),
            ],
        )

    def test_overlapping_findings_of_the_same_type_keep_the_first(self) -> None:
        later = self.report.findings.create(
            source=self.source,
            finding_type=FindingType.SIMILARITY,
            start_offset=self.similarity_finding.start_offset + 3,
            end_offset=self.similarity_finding.end_offset + 10,
            confidence_percent=Decimal("1.00"),
        )

        pieces = findings_in_paint_order(
            [later, self.similarity_finding], len(SAMPLE_TEXT)
        )

        self.assertEqual(
            [finding for _, _, finding in pieces if finding.finding_type == FindingType.SIMILARITY],
            [self.similarity_finding],
        )

    def test_viewer_paints_the_source_passage(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

        self.assertIn(
            ("metodologia aplicada", FindingType.SIMILARITY),
            [
                (segment.text, segment.finding_type)
                for segment in response.context["highlighted_segments"]
            ],
        )

    def test_pdf_cites_the_source(self) -> None:
        builder = _HighlightedDocumentPdfBuilder(report=self.report)

        _, cited_sources, word_counts = builder._render_body_markup(
            content=SAMPLE_TEXT,
            findings=list(self.report.findings.all()),
            sources_by_id={self.source.id: self.source},
        )

        self.assertEqual([source for _, source in cited_sources], [self.source])
        self.assertEqual(word_counts[self.source.id], 2)


class SourcePercentFormatTests(ReportsTestCaseMixin, TestCase):
    """
    El % de cada fuente sale con un decimal: 0,4 % y no 0 %.
    """

    def setUp(self) -> None:
        super().setUp()
        self.source.matched_percent = Decimal("0.40")
        self.source.save()

    def test_web_shows_one_decimal(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

        self.assertContains(response, "0,4%")

    def test_pdf_shows_one_decimal(self) -> None:
        from pypdf import PdfReader

        text = "".join(
            page.extract_text()
            for page in PdfReader(
                BytesIO(build_highlighted_document_pdf(self.report))
            ).pages
        )

        self.assertIn("0,4%", text)

    def test_pdf_rounds_like_the_web(self) -> None:
        builder = _HighlightedDocumentPdfBuilder(report=self.report)

        self.assertEqual(builder._format_source_percent(Decimal("0.25")), "0,3")
        self.assertEqual(builder._format_source_percent(Decimal("12.00")), "12,0")


class DownloadHighlightedDocumentViewTests(ReportsTestCaseMixin, TestCase):
    def test_owner_can_download_the_pdf(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:download_highlighted", kwargs={"pk": self.document.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def _download(self, user=None):
        if user is not None:
            self.client.force_login(user)

        return self.client.get(
            reverse("reports:download_highlighted", kwargs={"pk": self.document.id}),
        )

    def _staff(self, username, role, institution):
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="clave-segura-123",
            role=role,
            institution=institution,
        )

    def test_other_students_cannot_download_the_pdf(self) -> None:
        # REQ-CER-07: un estudiante ajeno recibe 403 (antes 404).
        self.assertEqual(self._download(self.other_student).status_code, 403)

    def test_staff_of_the_institution_can_download(self) -> None:
        for role in (UserRole.TEACHER, UserRole.DIRECTOR, UserRole.ADMIN):
            with self.subTest(role=role):
                staff = self._staff(f"staff-{role}", role, self.institution)

                response = self._download(staff)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")

    def test_staff_of_another_institution_cannot_download(self) -> None:
        other = Institution.objects.create(
            name="Otra institución",
            slug="otra-institucion-descarga",
        )

        for role in (UserRole.TEACHER, UserRole.DIRECTOR, UserRole.ADMIN):
            with self.subTest(role=role):
                staff = self._staff(f"ajeno-{role}", role, other)

                self.assertEqual(self._download(staff).status_code, 403)

    def test_staff_without_institution_cannot_download(self) -> None:
        teacher = self._staff("sin-inst", UserRole.TEACHER, None)

        self.assertEqual(self._download(teacher).status_code, 403)

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self._download()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/"))
        self.assertNotEqual(response["Content-Type"], "application/pdf")

    def test_superuser_can_download_from_any_institution(self) -> None:
        superuser = User.objects.create_superuser(
            username="root-descarga",
            email="root-descarga@example.com",
            password="clave-segura-123",
        )

        self.assertEqual(self._download(superuser).status_code, 200)

    def test_unknown_document_is_404(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse(
                "reports:download_highlighted",
                kwargs={"pk": "00000000-0000-0000-0000-000000000000"},
            )
        )

        self.assertEqual(response.status_code, 404)


class HighlightAlignmentTests(TestCase):
    """
    Auditoría senior, Hallazgo 1: los offsets de los hallazgos se calculan
    contra el texto FILTRADO, pero el visor y el PDF los pintaban sobre el
    texto CRUDO (portada/índice incluidos), desalineando los resaltados en
    casi todos los documentos reales.

    Arreglo (Opción B): se persiste el texto filtrado en
    `AnalysisReport.analyzed_content` y tanto el visor como el PDF pintan
    los resaltados sobre ESE texto, el mismo contra el que se calcularon
    los offsets. Repro del TEST 1 de la auditoría: carátula + índice de
    tablas al inicio, un hallazgo cerca del inicio del cuerpo y otro cerca
    del final (donde el desfase acumulado era peor).
    """

    COVER = (
        "UNIVERSIDAD NACIONAL DE EDUCACION\n"
        "FACULTAD DE EDUCACION\n"
        "ESCUELA PROFESIONAL DE EDUCACION PRIMARIA\n"
        "TESIS\n"
        "PARA OPTAR EL TITULO PROFESIONAL DE LICENCIADO\n"
        "AUTOR: Juan Perez\n"
        "ASESOR: Dr. Lopez\n"
        "CURSO: Investigacion"
    )
    INDEX_RUN = "\n".join(
        f"Tabla {n} Resultados de la dimension {n} .......... {10 + n}"
        for n in range(1, 6)
    )
    OPENING_FINDING = (
        "La investigacion analiza la relacion entre el acompanamiento "
        "familiar y el rendimiento academico de los estudiantes del nivel "
        "primario en instituciones educativas publicas del distrito."
    )
    FILLER = (
        "El desarrollo profesional docente en contextos rurales exige "
        "acompanamiento situado y sostenido en el tiempo por parte de los "
        "formadores de la institucion educativa. "
    ) * 30
    CLOSING_FINDING = (
        "Las conclusiones del estudio muestran que la retroalimentacion "
        "oportuna del docente incide directamente en la motivacion y el "
        "desempeno final de los estudiantes evaluados."
    )

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba-alineacion",
        )
        self.student = User.objects.create_user(
            username="alumno-alineacion",
            email="alumno-alineacion@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )
        self.other_student = User.objects.create_user(
            username="otro-alineacion",
            email="otro-alineacion@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

        self.raw_content = (
            f"{self.COVER}\n\n{self.INDEX_RUN}\n\n{self.OPENING_FINDING}"
            f"\n\n{self.FILLER}\n\n{self.CLOSING_FINDING}"
        )

        filtered = AcademicTextFilter().filter_for_similarity(
            content=self.raw_content,
        )
        self.filtered_content = filtered.content

        # Precondición del repro: el filtro sí debe haber quitado la
        # carátula y el índice, si no el test no reproduce nada.
        assert "UNIVERSIDAD" not in self.filtered_content
        assert "Tabla 1" not in self.filtered_content

        self.document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis con caratula e indice",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(self.raw_content),
            sha256_hash="hash-tesis-alineacion",
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(self.raw_content.encode("utf-8")),
            save=False,
        )
        self.document.save()

        DocumentText.objects.create(
            document=self.document,
            content=self.raw_content,
            word_count=len(self.raw_content.split()),
            character_count=len(self.raw_content),
        )

        opening_start = self.filtered_content.index(self.OPENING_FINDING)
        opening_end = opening_start + len(self.OPENING_FINDING)
        closing_start = self.filtered_content.index(self.CLOSING_FINDING)
        closing_end = closing_start + len(self.CLOSING_FINDING)

        # El hallazgo de cierre debe caer en el tramo final del texto
        # filtrado: es la posición donde el desfase acumulado por offsets
        # sin remapear era peor en el bug original.
        assert closing_start > len(self.filtered_content) * 0.8

        self.report = AnalysisReport.objects.create(
            document=self.document,
            similarity_percent=Decimal("40.00"),
            web_similarity_percent=Decimal("0.00"),
            internal_similarity_percent=Decimal("40.00"),
            ai_probability_percent=Decimal("55.00"),
            risk_level=ReportRiskLevel.HIGH,
            analyzed_content=self.filtered_content,
        )

        self.opening_finding = self.report.findings.create(
            source=None,
            finding_type=FindingType.SIMILARITY,
            start_offset=opening_start,
            end_offset=opening_end,
            text_excerpt=self.OPENING_FINDING,
            confidence_percent=Decimal("40.00"),
        )
        self.closing_finding = self.report.findings.create(
            source=None,
            finding_type=FindingType.AI_GENERATED,
            start_offset=closing_start,
            end_offset=closing_end,
            text_excerpt=self.CLOSING_FINDING,
            confidence_percent=Decimal("55.00"),
        )

    def test_resolve_analyzed_content_returns_the_persisted_filtered_text(
        self,
    ) -> None:
        resolved = resolve_analyzed_content(self.report)

        self.assertEqual(resolved, self.filtered_content)
        self.assertNotIn("UNIVERSIDAD", resolved)
        self.assertNotIn("Tabla 1", resolved)

    def test_viewer_highlights_land_exactly_on_marked_passages(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id}),
        )

        segments = response.context["highlighted_segments"]

        similarity_texts = [
            segment.text
            for segment in segments
            if segment.finding_type == FindingType.SIMILARITY
        ]
        ai_texts = [
            segment.text
            for segment in segments
            if segment.finding_type == FindingType.AI_GENERATED
        ]

        # El resaltado cae EXACTO sobre el párrafo marcado, tanto al inicio
        # del cuerpo como al final del documento — antes del arreglo, el
        # resaltado del final caía sobre basura de carátula/índice por el
        # desfase acumulado de offsets.
        self.assertEqual(similarity_texts, [self.OPENING_FINDING])
        self.assertEqual(ai_texts, [self.CLOSING_FINDING])

        full_rendered_text = "".join(segment.text for segment in segments)

        self.assertNotIn("UNIVERSIDAD", full_rendered_text)
        self.assertNotIn("Tabla 1", full_rendered_text)

    def test_highlighted_pdf_builds_without_error_from_filtered_text(
        self,
    ) -> None:
        pdf_bytes = build_highlighted_document_pdf(report=self.report)

        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 1000)


class ResolveAnalyzedContentFallbackTests(TestCase):
    """
    Reportes generados ANTES de que existiera `analyzed_content` no deben
    reventar el visor: se reconstruye el texto filtrado al vuelo desde el
    texto crudo con el filtro académico actual.
    """

    COVER = (
        "UNIVERSIDAD NACIONAL DE EDUCACION\n"
        "FACULTAD DE EDUCACION\n"
        "TESIS\n"
        "PARA OPTAR EL TITULO PROFESIONAL DE LICENCIADO\n"
        "AUTOR: Juan Perez\n"
        "ASESOR: Dr. Lopez\n"
        "LIMA - PERU 2024"
    )
    BODY = (
        "El acompanamiento pedagogico incide en el desempeno docente "
        "dentro del aula durante el periodo evaluado por la institucion. "
    ) * 5

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba-fallback",
        )
        self.student = User.objects.create_user(
            username="alumno-fallback",
            email="alumno-fallback@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

        self.raw_content = f"{self.COVER}\n\n{self.BODY}"

        self.document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis reporte antiguo",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(self.raw_content),
            sha256_hash="hash-tesis-fallback",
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(self.raw_content.encode("utf-8")),
            save=False,
        )
        self.document.save()

        DocumentText.objects.create(
            document=self.document,
            content=self.raw_content,
            word_count=len(self.raw_content.split()),
            character_count=len(self.raw_content),
        )

        # Reporte "antiguo": analyzed_content vacío, como quedaron todos los
        # reportes generados antes de este arreglo.
        self.report = AnalysisReport.objects.create(
            document=self.document,
            similarity_percent=Decimal("10.00"),
            risk_level=ReportRiskLevel.LOW,
            analyzed_content="",
        )

    def test_falls_back_to_refiltering_raw_text_on_the_fly(self) -> None:
        resolved = resolve_analyzed_content(self.report)

        self.assertNotIn("UNIVERSIDAD", resolved)
        self.assertIn("acompanamiento pedagogico", resolved)

    def test_viewer_does_not_break_for_a_report_without_analyzed_content(
        self,
    ) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id}),
        )

        self.assertEqual(response.status_code, 200)


class ExclusionOptionsDisplayTests(ReportsTestCaseMixin, TestCase):
    """
    El reporte muestra qué se excluyó según las opciones GUARDADAS, y la
    similitud no se pinta dentro de una cita excluida.
    """

    def _quote_inside_similarity_finding(self) -> tuple[int, int]:
        # "metodologia aplicada" es el hallazgo de similitud; se marca
        # "aplicada" como cita excluida.
        quote_start = SAMPLE_TEXT.index("aplicada")
        quote_end = quote_start + len("aplicada")
        self.report.analyzed_content = SAMPLE_TEXT
        self.report.summary = {"excluded_quote_ranges": [[quote_start, quote_end]]}
        self.report.save()
        return quote_start, quote_end

    def test_label_reflects_saved_options(self) -> None:
        self.assertEqual(self.report.excluded_options_label, "citas, bibliografía")

        self.report.exclude_quotes = False
        self.assertEqual(self.report.excluded_options_label, "bibliografía")

        self.report.exclude_bibliography = False
        self.assertEqual(self.report.excluded_options_label, "")

    def test_detail_shows_excluded_legend(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", kwargs={"pk": self.document.id}))

        self.assertContains(response, "Excluidas: citas, bibliografía")

    def test_legacy_report_does_not_claim_quotes_excluded(self) -> None:
        self.report.exclude_quotes = False
        self.report.save()
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", kwargs={"pk": self.document.id}))

        self.assertContains(response, "Excluidas: bibliografía")
        self.assertNotContains(response, "Excluidas: citas")

    def test_viewer_does_not_paint_similarity_inside_excluded_quote(self) -> None:
        self._quote_inside_similarity_finding()
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", kwargs={"pk": self.document.id}))
        segments = response.context["highlighted_segments"]

        painted = [
            segment.text
            for segment in segments
            if segment.finding_type == FindingType.SIMILARITY
        ]
        self.assertEqual(painted, ["metodologia "])
        self.assertIn(
            "aplicada",
            [segment.text for segment in segments if segment.finding_type is None],
        )

    def test_quote_ranges_ignored_when_quotes_were_not_excluded(self) -> None:
        self._quote_inside_similarity_finding()
        self.report.exclude_quotes = False
        self.report.save()

        self.assertEqual(self.report.excluded_quote_ranges, [])

    def test_pdf_still_builds_with_excluded_quote(self) -> None:
        self._quote_inside_similarity_finding()

        segments = _HighlightedDocumentPdfBuilder(report=self.report)._build_segments(
            content=SAMPLE_TEXT,
            findings=list(self.report.findings.order_by("start_offset")),
        )
        painted = [
            text for text, finding_type, _ in segments
            if finding_type == FindingType.SIMILARITY
        ]
        self.assertEqual(painted, ["metodologia "])

        pdf = build_highlighted_document_pdf(self.report)

        self.assertTrue(pdf.startswith(b"%PDF"))


class ExclusionFlagsMigrationTests(TransactionTestCase):
    """
    Los reportes existentes se calcularon sin excluir citas: tras la
    migración deben quedar con exclude_quotes=False (para no mostrar
    "Excluidas: citas"), y los nuevos con True por defecto.
    """

    migrate_from = ("reports", "0003_analysisreport_analyzed_content")
    migrate_to = ("reports", "0004_analysisreport_exclusion_flags")

    def test_existing_reports_get_exclude_quotes_false(self) -> None:
        # El estado "antes" debe incluir el resto de las apps en su última
        # migración (p. ej. Institution.email_domain); solo reports retrocede.
        executor = MigrationExecutor(connection)
        before = [
            node
            for node in executor.loader.graph.leaf_nodes()
            if node[0] != "reports"
        ] + [self.migrate_from]
        executor.migrate(before)
        old_apps = executor.loader.project_state(before).apps

        Institution = old_apps.get_model("core", "Institution")
        User = old_apps.get_model("accounts", "User")
        Document = old_apps.get_model("documents", "Document")
        OldReport = old_apps.get_model("reports", "AnalysisReport")

        institution = Institution.objects.create(
            name="Instituto",
            slug="instituto",
            email_domain="",
        )
        user = User.objects.create(
            username="alumno-migracion",
            email="alumno-migracion@example.com",
            institution=institution,
        )
        document = Document.objects.create(
            institution=institution,
            owner=user,
            uploaded_by=user,
            title="Documento previo",
            original_filename="previo.pdf",
            original_file="private/previo.pdf",
            mime_type="application/pdf",
            file_size_bytes=10,
            sha256_hash="hash-previo",
        )
        old_report = OldReport.objects.create(document=document)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        after = [node for node in before if node != self.migrate_from] + [
            self.migrate_to
        ]
        executor.migrate(after)
        new_apps = executor.loader.project_state(after).apps
        MigratedReport = new_apps.get_model("reports", "AnalysisReport")

        migrated = MigratedReport.objects.get(pk=old_report.pk)
        self.assertFalse(migrated.exclude_quotes)
        self.assertTrue(migrated.exclude_bibliography)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

        self.assertTrue(AnalysisReport._meta.get_field("exclude_quotes").default)


class BackfillAnalyzedContentCommandTests(TestCase):
    """
    `backfill_analyzed_content` congela en los reportes antiguos el texto
    filtrado que hoy se reconstruye al vuelo, para que un cambio posterior
    del extractor o del filtro no desalinee sus resaltados.
    """

    COVER = ResolveAnalyzedContentFallbackTests.COVER
    BODY = ResolveAnalyzedContentFallbackTests.BODY

    def setUp(self) -> None:
        institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba-backfill",
        )
        student = User.objects.create_user(
            username="alumno-backfill",
            email="alumno-backfill@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=institution,
        )
        self.raw_content = f"{self.COVER}\n\n{self.BODY}"

        def make_document(suffix: str) -> Document:
            return Document.objects.create(
                institution=institution,
                owner=student,
                uploaded_by=student,
                title=f"Tesis {suffix}",
                original_filename=f"{suffix}.pdf",
                original_file=f"private/{suffix}.pdf",
                mime_type="application/pdf",
                file_size_bytes=10,
                sha256_hash=f"hash-backfill-{suffix}",
            )

        legacy_document = make_document("antiguo")
        DocumentText.objects.create(
            document=legacy_document,
            content=self.raw_content,
            word_count=len(self.raw_content.split()),
            character_count=len(self.raw_content),
        )
        self.legacy_report = AnalysisReport.objects.create(
            document=legacy_document,
            risk_level=ReportRiskLevel.LOW,
            analyzed_content="",
        )

        self.current_report = AnalysisReport.objects.create(
            document=make_document("actual"),
            risk_level=ReportRiskLevel.LOW,
            analyzed_content="texto ya congelado por el análisis",
        )

        self.no_text_report = AnalysisReport.objects.create(
            document=make_document("sin-texto"),
            risk_level=ReportRiskLevel.LOW,
            analyzed_content="",
        )

    def _run(self, *args: str) -> str:
        output = StringIO()
        call_command("backfill_analyzed_content", *args, stdout=output)
        return output.getvalue()

    def test_dry_run_is_the_default_and_writes_nothing(self) -> None:
        output = self._run()

        self.legacy_report.refresh_from_db()
        self.assertEqual(self.legacy_report.analyzed_content, "")
        self.assertIn("CONGELARÍA", output)
        self.assertIn("No se escribió nada", output)

    def test_apply_freezes_the_current_filtered_text(self) -> None:
        expected = resolve_analyzed_content(self.legacy_report)

        self._run("--apply")

        self.legacy_report.refresh_from_db()
        self.assertEqual(self.legacy_report.analyzed_content, expected)
        self.assertNotIn("UNIVERSIDAD", self.legacy_report.analyzed_content)
        self.assertIn(
            "acompanamiento pedagogico",
            self.legacy_report.analyzed_content,
        )

    def test_apply_does_not_touch_reports_already_frozen_or_without_text(
        self,
    ) -> None:
        self._run("--apply")

        self.current_report.refresh_from_db()
        self.no_text_report.refresh_from_db()
        self.assertEqual(
            self.current_report.analyzed_content,
            "texto ya congelado por el análisis",
        )
        self.assertEqual(self.no_text_report.analyzed_content, "")

    def test_frozen_text_survives_a_later_change_of_the_extracted_text(
        self,
    ) -> None:
        self._run("--apply")
        frozen = AnalysisReport.objects.get(pk=self.legacy_report.pk).analyzed_content

        # Simula un extractor nuevo que cambia el texto crudo guardado.
        DocumentText.objects.filter(document=self.legacy_report.document).update(
            content="Texto completamente distinto. " * 10,
        )

        report = AnalysisReport.objects.get(pk=self.legacy_report.pk)
        self.assertEqual(resolve_analyzed_content(report), frozen)


def _synthetic_words(seed: int, count: int) -> list[str]:
    """
    Vocabulario propio por semilla: ningún tramo de 8 palabras se repite
    entre semillas, así los % esperados son exactos.
    """
    rng = random.Random(seed)
    vocabulary = [f"s{seed}w{index}" for index in range(400)]
    return [rng.choice(vocabulary) for _ in range(count)]


class SourceExclusionTestMixin:
    """
    Reporte real del motor span: el documento tiene 200 palabras contables;
    las fuentes se arman con tramos de `self.copied` para controlar el
    solapamiento.
    """

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba",
        )
        self.other_institution = Institution.objects.create(
            name="Otro instituto",
            slug="otro-instituto",
        )
        self.student = self._user("alumno1", UserRole.STUDENT, self.institution)
        self.source_author = self._user("alumno2", UserRole.STUDENT, self.institution)
        self.teacher = self._user("docente1", UserRole.TEACHER, self.institution)
        self.director = self._user("director1", UserRole.DIRECTOR, self.institution)
        self.foreign_teacher = self._user(
            "docente2", UserRole.TEACHER, self.other_institution
        )

        self.document = self._document(
            owner=self.student,
            title="Proyecto analizado",
            sha256_hash="hash-proyecto-analizado",
        )
        self.document.uploaded_by = self.teacher
        self.document.save(update_fields=["uploaded_by"])

        self.copied = _synthetic_words(21, 200)
        self.original = _synthetic_words(22, 200)

    def _user(self, username: str, role: str, institution: Institution) -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="clave-segura-123",
            role=role,
            institution=institution,
        )

    def _document(
        self,
        owner: User,
        title: str,
        sha256_hash: str,
    ) -> Document:
        document = Document(
            institution=self.institution,
            owner=owner,
            uploaded_by=owner,
            title=title,
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(SAMPLE_TEXT),
            sha256_hash=sha256_hash,
            status=DocumentStatus.COMPLETED,
        )
        document.original_file.save(
            "documento.pdf",
            ContentFile(SAMPLE_TEXT.encode("utf-8")),
            save=False,
        )
        document.save()
        return document

    def _internal_source(
        self,
        words: list[str],
        title: str,
        sha256_hash: str | None = None,
    ) -> Document:
        source = self._document(
            owner=self.source_author,
            title=title,
            sha256_hash=sha256_hash or f"hash-{title}",
        )
        DocumentKnowledgeIndexer().index(
            DocumentText(document=source),
            content=" ".join(words),
        )
        return source

    def _web_page(self, words: list[str], title: str = "Página web") -> WebPageContent:
        return WebPageContent(
            title=title,
            url=f"https://example.com/{title.lower().replace(' ', '-')}",
            domain="example.com",
            text=" ".join(words),
            source_format="html",
        )

    def _analyze(
        self,
        copied_words: int,
        web_pages: list[WebPageContent] = (),
        ai_percent: Decimal = Decimal("0.00"),
    ) -> AnalysisReport:
        """
        Misma persistencia que `DocumentAnalysisService.execute` con el
        motor span, sin extraer el archivo ni consultar Brave.
        """
        content = " ".join(
            self.copied[:copied_words] + self.original[: 200 - copied_words]
        )
        service = DocumentAnalysisService(requested_by=self.teacher)
        similarity = service._run_similarity(
            document=self.document,
            analysis_content=content,
            quoted_ranges=[],
            collect_web_pages=lambda _covered: list(web_pages),
            engine="span",
        )
        report = service._save_report(
            document=self.document,
            job=None,
            total_similarity_percent=similarity.total_similarity_percent,
            internal_similarity_percent=similarity.internal_result.similarity_percent,
            web_similarity_percent=similarity.web_result.web_similarity_percent,
            ai_probability_percent=ai_percent,
            ai_detector="spanish-heuristic-v1",
            excluded_sections=[],
            analyzed_content=content,
            exclude_quotes=False,
            similarity=similarity,
        )
        SpanReportWriter().write(
            report=report,
            result=similarity.span_result,
            reset=True,
        )
        report.refresh_from_db()
        return report

    def _source(self, report: AnalysisReport, title: str) -> ReportSource:
        return report.sources.get(title=title)

    def _percents(self, report: AnalysisReport) -> tuple[Decimal, Decimal, Decimal]:
        report.refresh_from_db()
        return (
            report.similarity_percent,
            report.internal_similarity_percent,
            report.web_similarity_percent,
        )


class SourceExclusionRecalculationTests(SourceExclusionTestMixin, TestCase):
    def test_single_source_exclusion_drops_its_words(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        self.assertEqual(
            self._percents(report),
            (Decimal("50.00"), Decimal("0.00"), Decimal("50.00")),
        )
        self.assertEqual(report.risk_level, ReportRiskLevel.CRITICAL)
        self.assertEqual(report.original_similarity_percent, Decimal("50.00"))

        source = self._source(report, "Página web")
        self.assertEqual(source.word_count, 100)

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=source.id,
            reason="Documento de prueba",
        )

        report.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(
            self._percents(report),
            (Decimal("0.00"), Decimal("0.00"), Decimal("0.00")),
        )
        self.assertEqual(report.risk_level, ReportRiskLevel.LOW)
        self.assertEqual(report.original_similarity_percent, Decimal("50.00"))
        self.assertTrue(source.is_excluded)
        self.assertEqual(source.excluded_by, self.teacher)
        self.assertIsNotNone(source.excluded_at)
        self.assertEqual(source.exclusion_reason, "Documento de prueba")
        # Conserva el % que tenía para mostrarlo en "Fuentes excluidas".
        self.assertEqual(source.matched_percent, Decimal("50.00"))
        self.assertFalse(
            report.findings.filter(finding_type=FindingType.SIMILARITY).exists()
        )

    def test_overlapping_sources_reassign_words_to_the_next_source(self) -> None:
        # Interna: palabras 0-99. Web: palabras 50-149 (se pisan en 50-99).
        self._internal_source(self.copied[:100], title="Coautoría")
        report = self._analyze(
            copied_words=150,
            web_pages=[self._web_page(self.copied[50:150])],
        )
        internal = self._source(report, "Coautoría")
        web = self._source(report, "Página web")

        self.assertEqual(
            self._percents(report),
            (Decimal("75.00"), Decimal("50.00"), Decimal("25.00")),
        )
        self.assertEqual((internal.word_count, web.word_count), (100, 50))

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=internal.id,
            reason="Coautoría declarada",
        )

        web.refresh_from_db()
        self.assertEqual(
            self._percents(report),
            (Decimal("50.00"), Decimal("0.00"), Decimal("50.00")),
        )
        self.assertEqual(web.word_count, 100)
        self.assertEqual(web.matched_percent, Decimal("50.00"))

        findings = list(
            report.findings.filter(finding_type=FindingType.SIMILARITY).order_by(
                "start_offset"
            )
        )
        self.assertEqual({finding.source_id for finding in findings}, {web.id})
        self.assertEqual(
            findings[0].start_offset,
            report.analyzed_content.index(self.copied[50]),
        )

    def test_excluded_winner_hands_words_to_a_source_that_had_none(self) -> None:
        # La interna cubre todo lo que cubre la web: la web no gana palabras
        # y no aparece hasta que se excluye la interna.
        self._internal_source(self.copied[:100], title="Documento de prueba")
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:60])],
        )
        self.assertFalse(report.sources.filter(title="Página web").exists())

        service = ReportSourceExclusionService(requested_by=self.teacher)
        internal = self._source(report, "Documento de prueba")
        service.exclude(
            document_id=self.document.id,
            source_id=internal.id,
            reason="Documento de prueba",
        )

        self.assertEqual(
            self._percents(report),
            (Decimal("30.00"), Decimal("0.00"), Decimal("30.00")),
        )
        self.assertEqual(self._source(report, "Página web").word_count, 60)

        service.restore(document_id=self.document.id, source_id=internal.id)

        self.assertEqual(
            self._percents(report),
            (Decimal("50.00"), Decimal("50.00"), Decimal("0.00")),
        )
        self.assertFalse(report.sources.filter(title="Página web").exists())

    def test_restore_returns_the_original_percentages(self) -> None:
        self._internal_source(self.copied[:100], title="Coautoría")
        report = self._analyze(
            copied_words=150,
            web_pages=[self._web_page(self.copied[50:150])],
        )
        original = self._percents(report)
        original_findings = sorted(
            report.findings.filter(finding_type=FindingType.SIMILARITY).values_list(
                "source__title", "start_offset", "end_offset"
            )
        )
        service = ReportSourceExclusionService(requested_by=self.teacher)
        internal = self._source(report, "Coautoría")
        web = self._source(report, "Página web")

        service.exclude(
            document_id=self.document.id,
            source_id=internal.id,
            reason="Coautoría declarada",
        )
        service.exclude(
            document_id=self.document.id,
            source_id=web.id,
            reason="Página del propio autor",
        )
        self.assertEqual(
            self._percents(report),
            (Decimal("0.00"), Decimal("0.00"), Decimal("0.00")),
        )

        service.restore(document_id=self.document.id, source_id=web.id)
        service.restore(document_id=self.document.id, source_id=internal.id)

        internal.refresh_from_db()
        self.assertEqual(self._percents(report), original)
        self.assertEqual(report.risk_level, ReportRiskLevel.CRITICAL)
        self.assertFalse(internal.is_excluded)
        self.assertIsNone(internal.excluded_by)
        self.assertEqual(internal.exclusion_reason, "")
        self.assertEqual(
            sorted(
                report.findings.filter(
                    finding_type=FindingType.SIMILARITY
                ).values_list("source__title", "start_offset", "end_offset")
            ),
            original_findings,
        )

    def test_reason_is_required(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        source = self._source(report, "Página web")

        with self.assertRaisesMessage(
            ReportSourceExclusionError, "Indica el motivo para excluir la fuente."
        ):
            ReportSourceExclusionService(requested_by=self.teacher).exclude(
                document_id=self.document.id,
                source_id=source.id,
                reason="   ",
            )

        source.refresh_from_db()
        self.assertFalse(source.is_excluded)

    def test_report_without_alignment_cannot_exclude(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        report.span_alignment = {}
        report.save(update_fields=["span_alignment"])

        with self.assertRaisesMessage(
            ReportSourceExclusionError,
            "Vuelve a analizar el documento para poder excluir fuentes.",
        ):
            ReportSourceExclusionService(requested_by=self.teacher).exclude(
                document_id=self.document.id,
                source_id=self._source(report, "Página web").id,
                reason="Documento de prueba",
            )

    def test_exclusion_revokes_the_issued_certificate(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        certificate = Certificate.objects.create(
            report=report,
            code="IPT-2026-ABCD1234",
            verification_hash="a" * 64,
            issued_by=self.teacher,
            is_active=True,
        )

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self._source(report, "Página web").id,
            reason="Documento de prueba",
        )

        certificate.refresh_from_db()
        self.assertFalse(certificate.is_active)
        self.assertIsNotNone(certificate.revoked_at)
        self.assertEqual(
            certificate.revoke_reason,
            "Reporte recalculado por exclusión de fuentes",
        )

    def test_recalculated_risk_unlocks_the_certificate(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        certificates = CertificateGenerationService(
            issued_by=self.teacher,
            absolute_base_url="http://testserver/",
        )

        with self.assertRaises(ValidationError):
            certificates.execute(document_id=self.document.id)

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self._source(report, "Página web").id,
            reason="Documento de prueba",
        )

        certificate = certificates.execute(document_id=self.document.id)
        self.assertTrue(certificate.is_active)

    def test_pdf_lists_excluded_sources_with_their_reason(self) -> None:
        from io import BytesIO

        from pypdf import PdfReader

        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self._source(report, "Página web").id,
            reason="Documento de prueba",
        )

        report.refresh_from_db()
        reader = PdfReader(BytesIO(build_highlighted_document_pdf(report=report)))
        text = " ".join(page.extract_text() for page in reader.pages)

        self.assertIn("FUENTES EXCLUIDAS: 1", text)
        self.assertIn("Motivo: Documento de prueba", text)


class CommonTextReportTests(SourceExclusionTestMixin, TestCase):
    """
    50 palabras repetidas por dos fuentes (texto común con N=2) y 50 más
    copiadas de una sola.
    """

    def setUp(self) -> None:
        super().setUp()
        template, copied = self.copied[:50], self.copied[50:100]
        self._internal_source(template, "Fuente de plantilla")
        other_author = self._user("alumno3", UserRole.STUDENT, self.institution)
        source = self._internal_source(template, "Otra plantilla")
        source.owner = other_author
        source.save(update_fields=["owner"])
        self._internal_source(copied, "Fuente copiada")

    def _common_report(self) -> AnalysisReport:
        with override_settings(SIMILARITY_COMMON_TEXT_MIN_DOCS=2):
            return self._analyze(copied_words=100)

    def test_common_text_is_left_out_and_stored(self) -> None:
        report = self._common_report()
        end = len(" ".join(self.copied[:50]))

        self.assertEqual(report.similarity_percent, Decimal("25.00"))
        self.assertEqual(report.common_text_ranges, [(0, end)])
        self.assertEqual(
            list(report.sources.values_list("title", flat=True)),
            ["Fuente copiada"],
        )

    def test_recalculation_does_not_bring_common_text_back(self) -> None:
        report = self._common_report()
        source = self._source(report, "Fuente copiada")

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=source.id,
            reason="Fuente autorizada",
        )

        self.assertEqual(self._percents(report)[0], Decimal("0.00"))
        self.assertEqual(len(report.common_text_ranges), 1)

    def test_viewer_marks_common_text_in_grey(self) -> None:
        report = self._common_report()
        self.client.force_login(self.teacher)

        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

        self.assertContains(response, "Gris: texto común")
        grey = [
            segment.text
            for segment in response.context["highlighted_segments"]
            if segment.finding_type == "COMMON_TEXT"
        ]
        self.assertEqual(grey, [report.analyzed_content[:report.common_text_ranges[0][1]]])

    def test_pdf_marks_common_text(self) -> None:
        report = self._common_report()
        builder = _HighlightedDocumentPdfBuilder(report=report)

        markup, _, _ = builder._render_body_markup(
            content=report.analyzed_content,
            findings=list(report.findings.all()),
            sources_by_id={source.id: source for source in report.sources.all()},
        )

        self.assertIn(builder.COMMON_TEXT_BACKGROUND, markup)
        self.assertTrue(build_highlighted_document_pdf(report).startswith(b"%PDF"))

    def test_report_without_common_text_key_has_no_ranges(self) -> None:
        report = self._analyze(copied_words=100)
        report.span_alignment.pop("common_text")

        self.assertEqual(report.common_text_ranges, [])


class SourceExclusionPermissionTests(SourceExclusionTestMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        self.source = self._source(self.report, "Página web")
        self.url = reverse(
            "reports:exclude_source",
            kwargs={"pk": self.document.id, "source_pk": self.source.id},
        )

    def _post_as(self, user: User):
        self.client.force_login(user)
        return self.client.post(self.url, {"reason": "Documento de prueba"})

    def test_student_cannot_exclude(self) -> None:
        response = self._post_as(self.student)

        self.assertEqual(response.status_code, 403)
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_excluded)
        self.assertEqual(self._percents(self.report)[0], Decimal("50.00"))

    def test_teacher_from_another_institution_cannot_exclude(self) -> None:
        response = self._post_as(self.foreign_teacher)

        self.assertEqual(response.status_code, 403)
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_excluded)

    def test_teacher_of_the_same_institution_excludes(self) -> None:
        response = self._post_as(self.teacher)

        self.assertRedirects(
            response,
            reverse("reports:detail", kwargs={"pk": self.document.id}),
            fetch_redirect_response=False,
        )
        self.source.refresh_from_db()
        self.assertTrue(self.source.is_excluded)
        self.assertEqual(self._percents(self.report)[0], Decimal("0.00"))

    def test_director_of_the_same_institution_excludes(self) -> None:
        ReportSourceExclusionService(requested_by=self.director).exclude(
            document_id=self.document.id,
            source_id=self.source.id,
            reason="Documento de prueba",
        )

        self.source.refresh_from_db()
        self.assertTrue(self.source.is_excluded)

    def test_student_restore_is_also_forbidden(self) -> None:
        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self.source.id,
            reason="Documento de prueba",
        )
        self.client.force_login(self.student)

        response = self.client.post(
            reverse(
                "reports:restore_source",
                kwargs={"pk": self.document.id, "source_pk": self.source.id},
            )
        )

        self.assertEqual(response.status_code, 403)
        self.source.refresh_from_db()
        self.assertTrue(self.source.is_excluded)

    def test_empty_reason_shows_a_message(self) -> None:
        self.client.force_login(self.teacher)

        response = self.client.post(self.url, {"reason": ""}, follow=True)

        self.assertContains(response, "Indica el motivo para excluir la fuente.")
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_excluded)

    def test_button_is_hidden_from_the_student(self) -> None:
        detail = reverse("reports:detail", kwargs={"pk": self.document.id})

        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(detail), "Excluir fuente")

        self.client.force_login(self.teacher)
        self.assertContains(self.client.get(detail), "Excluir fuente")


class ResubmissionWarningTests(SourceExclusionTestMixin, TestCase):
    def _detail_as_teacher(self):
        self.client.force_login(self.teacher)
        return self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )

    def test_same_hash_source_is_flagged_even_with_low_coverage(self) -> None:
        self._internal_source(
            self.copied[:40],
            title="Original con mismo archivo",
            sha256_hash=self.document.sha256_hash,
        )
        report = self._analyze(copied_words=40)
        source = self._source(report, "Original con mismo archivo")

        self.assertTrue(source.is_possible_resubmission)
        self.assertTrue(source.resubmission["same_hash"])
        self.assertEqual(source.resubmission["coverage_percent"], "20.00")
        self.assertEqual(source.resubmission["original_owner"], "alumno2")
        self.assertEqual(resubmission_warning_sources([source]), [source])

        response = self._detail_as_teacher()
        self.assertContains(response, "Posible reenvío del mismo trabajo")
        self.assertContains(response, "archivo idéntico")

        # No se excluye sola; y si el docente la excluye, el hash sigue
        # avisando arriba.
        self.assertFalse(source.is_excluded)
        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=source.id,
            reason="Reenvío revisado",
        )
        source.refresh_from_db()
        self.assertEqual(resubmission_warning_sources([source]), [source])
        self.assertContains(self._detail_as_teacher(), "fuente excluida")

    def test_source_covering_95_percent_is_flagged(self) -> None:
        self._internal_source(self.copied[:192], title="Trabajo de otra cuenta")
        report = self._analyze(copied_words=192)
        source = self._source(report, "Trabajo de otra cuenta")

        self.assertTrue(source.is_possible_resubmission)
        self.assertFalse(source.resubmission["same_hash"])
        self.assertEqual(source.resubmission["coverage_percent"], "96.00")
        self.assertFalse(source.is_excluded)
        self.assertContains(
            self._detail_as_teacher(), "cubre el 96.00% del documento"
        )

        # Excluida por cobertura (sin mismo hash): deja de avisar arriba.
        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=source.id,
            reason="Coautoría",
        )
        source.refresh_from_db()
        self.assertTrue(source.is_possible_resubmission)
        self.assertEqual(resubmission_warning_sources([source]), [])

    def test_source_below_95_percent_is_not_flagged(self) -> None:
        self._internal_source(self.copied[:180], title="Trabajo parecido")
        report = self._analyze(copied_words=180)
        source = self._source(report, "Trabajo parecido")

        self.assertEqual(source.word_count, 180)
        self.assertFalse(source.is_possible_resubmission)
        self.assertNotContains(
            self._detail_as_teacher(), "Posible reenvío del mismo trabajo"
        )

    def test_coverage_counts_words_lost_to_another_source(self) -> None:
        # La web (192 palabras) gana casi todo; a la interna solo le quedan
        # 8 palabras, pero cubre el 95.5%: se marca por su cobertura bruta,
        # no por las palabras atribuidas.
        self._internal_source(self.copied[:191], title="Trabajo completo")
        report = self._analyze(
            copied_words=200,
            web_pages=[self._web_page(self.copied[8:200], title="Copia web")],
        )
        internal = self._source(report, "Trabajo completo")

        self.assertEqual(internal.word_count, 8)
        self.assertEqual(internal.resubmission["coverage_percent"], "95.50")
        self.assertEqual(
            [
                source.title
                for source in report.sources.all()
                if source.is_possible_resubmission
            ],
            ["Trabajo completo"],
        )


class RiskLevelIgnoresAiTests(SourceExclusionTestMixin, TestCase):
    """El riesgo depende solo de la similitud; el % de IA no lo altera."""

    def test_resolve_risk_level_uses_only_similarity(self) -> None:
        cases = [
            (Decimal("0.00"), ReportRiskLevel.LOW),
            (Decimal("14.99"), ReportRiskLevel.LOW),
            (Decimal("15.00"), ReportRiskLevel.MEDIUM),
            (Decimal("30.00"), ReportRiskLevel.HIGH),
            (Decimal("50.00"), ReportRiskLevel.CRITICAL),
        ]
        for similarity, expected in cases:
            with self.subTest(similarity=similarity):
                self.assertEqual(resolve_risk_level(similarity), expected)

    def test_high_ai_with_no_similarity_is_low_risk(self) -> None:
        report = self._analyze(copied_words=0, ai_percent=Decimal("95.00"))

        self.assertEqual(report.ai_probability_percent, Decimal("95.00"))
        self.assertEqual(report.similarity_percent, Decimal("0.00"))
        self.assertEqual(report.risk_level, ReportRiskLevel.LOW)

    def test_ai_does_not_raise_risk_above_similarity_level(self) -> None:
        report = self._analyze(
            copied_words=40,
            web_pages=[self._web_page(self.copied[:40])],
            ai_percent=Decimal("100.00"),
        )

        self.assertEqual(report.similarity_percent, Decimal("20.00"))
        self.assertEqual(report.risk_level, ReportRiskLevel.MEDIUM)

    def test_recalculation_after_exclusion_ignores_ai(self) -> None:
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
            ai_percent=Decimal("90.00"),
        )
        self.assertEqual(report.risk_level, ReportRiskLevel.CRITICAL)

        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self._source(report, "Página web").id,
            reason="Documento de prueba",
        )

        report.refresh_from_db()
        self.assertEqual(report.ai_probability_percent, Decimal("90.00"))
        self.assertEqual(report.risk_level, ReportRiskLevel.LOW)

    def test_high_ai_low_similarity_report_can_be_certified(self) -> None:
        report = self._analyze(copied_words=0, ai_percent=Decimal("95.00"))
        self.document.status = DocumentStatus.COMPLETED
        self.document.save(update_fields=["status"])

        certificate = CertificateGenerationService(
            issued_by=self.teacher,
            absolute_base_url="http://testserver/",
        ).execute(document_id=self.document.id)

        self.assertEqual(certificate.report_id, report.id)


class AiDisclaimerDisplayTests(ReportsTestCaseMixin, TestCase):
    def test_detail_shows_ai_disclaimer(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", kwargs={"pk": self.document.id}))

        self.assertContains(response, AI_ESTIMATE_DISCLAIMER)

    def test_similarity_pdf_has_no_ai_content(self) -> None:
        # REQ-CER-03 / design §4.3-5: la IA va en su propio informe.
        from pypdf import PdfReader

        pdf_bytes = build_highlighted_document_pdf(report=self.report)
        text = " ".join(
            page.extract_text() for page in PdfReader(BytesIO(pdf_bytes)).pages
        )

        self.assertNotIn("IA estimada", text)
        self.assertNotIn("IA ESTIMADA", text)
        self.assertNotIn("Posible IA", text)
        self.assertNotIn(" ".join(AI_ESTIMATE_DISCLAIMER.split()), " ".join(text.split()))


class AiEstimateDetailTests(ReportsTestCaseMixin, TestCase):
    """T14 / REQ-CER-06: el detalle del reporte muestra lo mismo que AiEstimate."""

    def _detail(self) -> str:
        self.client.force_login(self.student)
        response = self.client.get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )
        self.assertEqual(response.status_code, 200)
        return " ".join(response.content.decode().split())

    def test_detalle_coincide_con_ai_estimate(self) -> None:
        from django.template.defaultfilters import floatformat

        from apps.reports.ai_estimate import build_ai_estimate

        self.report.ai_probability_percent = Decimal("37.46")
        self.report.engine_version = "motor-historico-1"
        self.report.save()
        estimate = build_ai_estimate(self.report)

        html = self._detail()

        self.assertIn(f"{floatformat(estimate.percent, 1)}%", html)
        self.assertIn(estimate.band_label, html)
        self.assertIn(estimate.version_label, html)
        self.assertIn("motor-historico-1", html)
        self.assertIn(" ".join(estimate.note.split()), html)
        self.assertIn(
            timezone.localtime(estimate.generated_at).strftime("%d/%m/%Y"), html
        )  # la página muestra la hora de America/Lima, no UTC

    def test_bandas_en_los_bordes_usan_40_y_70(self) -> None:
        casos = {
            "39.90": "Sin indicios concluyentes",
            "40.00": "Indicios: revisar con criterio docente",
            "69.90": "Indicios: revisar con criterio docente",
            "70.00": "Posible IA: revisar",
        }
        for valor, banda in casos.items():
            with self.subTest(valor=valor):
                self.report.ai_probability_percent = Decimal(valor)
                self.report.save()
                self.assertIn(banda, self._detail())

    def test_no_usa_umbrales_ni_textos_propios_anteriores(self) -> None:
        # 16 % era "moderado" con el umbral 15/30 antiguo.
        self.report.ai_probability_percent = Decimal("16.00")
        self.report.save()

        html = self._detail()

        self.assertNotIn("Nivel moderado", html)
        self.assertNotIn("Nivel alto", html)
        self.assertNotIn("Nivel bajo", html)
        self.assertIn("Sin indicios concluyentes", html)


class RecalculateRiskLevelsCommandTests(SourceExclusionTestMixin, TestCase):
    """`recalculate_risk_levels` corrige el riesgo heredado del % de IA."""

    def setUp(self) -> None:
        super().setUp()
        # Riesgo subido por IA (similitud baja) → debe quedar BAJO.
        self.ai_inflated = self._report(
            self.document, Decimal("5.00"), Decimal("85.00"), ReportRiskLevel.CRITICAL
        )
        # Ya consistente con su similitud → no cambia.
        self.consistent = self._report(
            self._document(self.student, "Consistente", "hash-consistente"),
            Decimal("35.00"),
            Decimal("70.00"),
            ReportRiskLevel.HIGH,
        )
        # Similitud media con riesgo ALTO por IA → MEDIO, ya certificado.
        self.certified = self._report(
            self._document(self.student, "Certificado", "hash-certificado"),
            Decimal("20.00"),
            Decimal("65.00"),
            ReportRiskLevel.HIGH,
        )
        Certificate.objects.create(
            report=self.certified,
            code="CERT-PRUEBA-1",
            verification_hash="hash-verificacion-prueba",
            issued_by=self.teacher,
        )

    def _report(
        self,
        document: Document,
        similarity: Decimal,
        ai: Decimal,
        risk: str,
    ) -> AnalysisReport:
        return AnalysisReport.objects.create(
            document=document,
            similarity_percent=similarity,
            web_similarity_percent=similarity,
            ai_probability_percent=ai,
            risk_level=risk,
        )

    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("recalculate_risk_levels", *args, stdout=out)
        return out.getvalue()

    def _levels(self) -> list[str]:
        return [
            AnalysisReport.objects.get(pk=report.pk).risk_level
            for report in (self.ai_inflated, self.consistent, self.certified)
        ]

    def test_dry_run_is_default_and_writes_nothing(self) -> None:
        output = self._run()

        self.assertIn("DRY-RUN", output)
        self.assertIn("2 de 3 reportes cambian", output)
        self.assertIn("Crítico → Bajo: 1", output)
        self.assertIn("Alto → Medio: 1", output)
        self.assertEqual(
            self._levels(),
            [ReportRiskLevel.CRITICAL, ReportRiskLevel.HIGH, ReportRiskLevel.HIGH],
        )
        self.assertEqual(self._run("--dry-run"), output)

    def test_lists_only_uncertified_reports_that_become_certifiable(self) -> None:
        output = self._run()

        self.assertIn("pasaría a permitirse: 1", output)
        self.assertIn(str(self.ai_inflated.id), output)
        self.assertNotIn(str(self.certified.id), output)
        self.assertNotIn(str(self.consistent.id), output)

    def test_flags_reports_still_blocked_by_other_rules(self) -> None:
        self.ai_inflated.is_final = False
        self.ai_inflated.save(update_fields=["is_final"])

        output = self._run()

        self.assertIn("aún no certificable: reporte no final", output)

    def test_apply_saves_new_levels_without_touching_certificates(self) -> None:
        certificate = Certificate.objects.get(report=self.certified)

        output = self._run("--apply")

        self.assertIn("2 reportes actualizados", output)
        self.assertEqual(
            self._levels(),
            [ReportRiskLevel.LOW, ReportRiskLevel.HIGH, ReportRiskLevel.MEDIUM],
        )
        self.assertEqual(Certificate.objects.count(), 1)
        self.assertEqual(
            Certificate.objects.get(pk=certificate.pk).report_id, self.certified.id
        )
        self.assertIn("0 de 3 reportes cambian", self._run())

    def test_uses_similarity_after_source_exclusion(self) -> None:
        self.ai_inflated.delete()
        report = self._analyze(
            copied_words=100,
            web_pages=[self._web_page(self.copied[:100])],
        )
        ReportSourceExclusionService(requested_by=self.teacher).exclude(
            document_id=self.document.id,
            source_id=self._source(report, "Página web").id,
            reason="Documento de prueba",
        )
        # Nivel viejo inconsistente: el recálculo debe usar el 0 % posterior
        # a la exclusión, no el 50 % original.
        AnalysisReport.objects.filter(pk=report.pk).update(
            risk_level=ReportRiskLevel.CRITICAL
        )

        self._run("--apply")

        report.refresh_from_db()
        self.assertEqual(report.similarity_percent, Decimal("0.00"))
        self.assertEqual(report.original_similarity_percent, Decimal("50.00"))
        self.assertEqual(report.risk_level, ReportRiskLevel.LOW)


class SourceDisplayTests(SourceExclusionTestMixin, TestCase):
    """
    La fuente se presenta por el título del trabajo, con el repositorio o
    dominio como subtítulo, en la web, el PDF y la verificación pública.
    """

    def _oai_record(self, source_url: str) -> OaiRecord:
        return OaiRecord.objects.create(
            repository_name="Repositorio UPeU",
            repository_domain="repositorio.upeu.edu.pe",
            oai_identifier="oai:repositorio.upeu.edu.pe:20.500.12840/4983",
            title="Aprendizaje cooperativo y la expresión oral del inglés",
            source_url=source_url,
            text_excerpt="Resumen.",
            normalized_text="resumen",
            harvested_at=timezone.now(),
        )

    def _write_oai(self, record: OaiRecord) -> ReportSource:
        report = AnalysisReport.objects.create(document=self.document)
        result = SpanAnalysisResult(
            similarity_percent=Decimal("5.00"),
            countable_word_count=200,
            sources=[
                SpanSourceResult(
                    source=SpanSource(
                        key=f"oai:{record.id}",
                        kind=SOURCE_KIND_OAI,
                        title=record.title,
                        segments=("texto",),
                        source_id=record.id,
                        owner_name=record.repository_name,
                        url=record.source_url,
                        domain=record.repository_name,
                    ),
                    word_count=10,
                    percent=Decimal("5.00"),
                )
            ],
        )
        SpanReportWriter().write(report=report, result=result, reset=True)
        return report.sources.get()

    def test_internal_source_shows_work_title_without_student_name(self) -> None:
        self._internal_source(self.copied[:60], title="Tesis sobre convivencia")
        source = self._source(self._analyze(copied_words=60), "Tesis sobre convivencia")

        self.assertEqual(source.display_title, "Tesis sobre convivencia")
        self.assertEqual(source.display_subtitle, "Repositorio interno")
        self.assertEqual(source.link_url, "")

        response = self._client_for(self.teacher).get(
            reverse("reports:detail", kwargs={"pk": self.document.id})
        )
        self.assertContains(response, "Tesis sobre convivencia")
        self.assertContains(response, "Repositorio interno")

    def test_web_source_links_to_page_with_domain_subtitle(self) -> None:
        page = self._web_page(self.copied[:60], title="Guía de convivencia")
        source = self._source(
            self._analyze(copied_words=60, web_pages=[page]), "Guía de convivencia"
        )

        self.assertEqual(source.display_subtitle, "example.com")
        self.assertEqual(source.link_url, page.url)

    def test_oai_source_links_to_record_url(self) -> None:
        record = self._oai_record("https://hdl.handle.net/20.500.12840/4983")
        source = self._write_oai(record)

        self.assertTrue(source.is_oai)
        self.assertEqual(source.display_title, record.title)
        self.assertEqual(source.display_subtitle, "Repositorio UPeU")
        self.assertEqual(source.link_url, record.source_url)

    def test_oai_source_without_url_falls_back_to_pdf(self) -> None:
        record = self._oai_record("")
        OaiFullText.objects.create(
            record=record,
            status=OaiFullTextStatus.OK,
            pdf_url="https://repositorio.upeu.edu.pe/bitstream/4983.pdf",
        )
        source = self._write_oai(record)

        self.assertEqual(
            source.link_url, "https://repositorio.upeu.edu.pe/bitstream/4983.pdf"
        )

    def test_pdf_lists_title_with_repository_subtitle(self) -> None:
        from pypdf import PdfReader

        self._internal_source(self.copied[:60], title="Tesis sobre convivencia")
        report = self._analyze(copied_words=60)
        text = "".join(
            page.extract_text()
            for page in PdfReader(BytesIO(build_highlighted_document_pdf(report))).pages
        )

        self.assertIn("Tesis sobre convivencia", text)
        self.assertIn("Repositorio interno", text)
        self.assertNotIn("Base interna", text)

    def test_verify_page_lists_sources(self) -> None:
        self._internal_source(self.copied[:60], title="Tesis sobre convivencia")
        report = self._analyze(copied_words=60)
        Certificate.objects.create(
            report=report,
            code="CERT-0001",
            verification_hash="a" * 64,
            issued_by=self.teacher,
        )

        response = self.client.get(
            reverse("certificates:verify", args=["a" * 64])
        )

        self.assertContains(response, "Certificado válido")
        # RN-CER-04: la fuente interna solo sale agregada, sin título.
        self.assertContains(response, "Base interna: 1 fuente")
        self.assertNotContains(response, "Tesis sobre convivencia")
        self.assertNotContains(response, "alumno2")

    def test_verify_page_unknown_hash(self) -> None:
        response = self.client.get(
            reverse("certificates:verify", args=["b" * 64])
        )

        self.assertContains(response, "Certificado no encontrado")

    def _client_for(self, user: User):
        self.client.force_login(user)
        return self.client


class BandasDePresentacionTests(SimpleTestCase):
    def test_banda_similitud_turnitin(self):
        from apps.reports.models import similarity_band

        casos = [
            ("0", "blue"), ("0.5", "green"), ("24.9", "green"), ("25", "yellow"),
            ("49.9", "yellow"), ("50", "orange"), ("74.9", "orange"),
            ("75", "red"), ("100", "red"),
        ]
        for valor, esperado in casos:
            self.assertEqual(similarity_band(valor)["key"], esperado, valor)

    def test_banda_clasificador_ia(self):
        from apps.reports.models import ai_classifier_band

        self.assertEqual(ai_classifier_band("39.9")["label"], "Sin indicios concluyentes")
        self.assertEqual(ai_classifier_band("40")["label"], "Indicios: revisar con criterio docente")
        self.assertEqual(ai_classifier_band("69.9")["label"], "Indicios: revisar con criterio docente")
        self.assertEqual(ai_classifier_band("70")["label"], "Posible IA: revisar")

    def test_ai_classifier_solo_con_breakdown_valido(self):
        from apps.reports.models import AnalysisReport

        r = AnalysisReport(summary={})
        self.assertIsNone(r.ai_classifier)
        r.summary = {"ai_score_breakdown": {"classifier": {"error": True}}}
        self.assertIsNone(r.ai_classifier)
        r.summary = {"ai_score_breakdown": {"classifier": {"ai_percent": "75.0"}}}
        self.assertEqual(r.ai_classifier["label"], "Posible IA: revisar")


class ResponsibleUseNoteTests(TestCase):
    """RN-CER-08: un único texto para la nota de uso responsable."""

    EXPECTED = (
        "IA estimada: valor probabilístico calculado por un modelo automático. "
        "No constituye prueba del uso de inteligencia artificial y requiere la "
        "revisión de un docente antes de cualquier decisión académica."
    )

    def test_nota_exacta(self) -> None:
        self.assertEqual(AI_RESPONSIBLE_USE_NOTE, self.EXPECTED)

    def test_alias_son_la_misma_nota(self) -> None:
        self.assertEqual(AI_ESTIMATE_DISCLAIMER, self.EXPECTED)
        self.assertEqual(AI_CLASSIFIER_NOTE, self.EXPECTED)
        self.assertEqual(AnalysisReport.AI_DISCLAIMER, self.EXPECTED)
        self.assertEqual(AnalysisReport.AI_CLASSIFIER_NOTE, self.EXPECTED)


class AiEstimateTests(TestCase):
    """REQ-CER-06 / RN-CER-03: AiEstimate como fuente única de la IA."""

    def _report(self, **kwargs) -> AnalysisReport:
        return AnalysisReport(
            ai_probability_percent=Decimal("40.00"),
            generated_at=timezone.now(),
            **kwargs,
        )

    def test_usa_porcentaje_del_clasificador_si_existe(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        report = self._report(
            summary={"ai_score_breakdown": {"classifier": {"ai_percent": "62.5"}}}
        )
        self.assertEqual(build_ai_estimate(report).percent, Decimal("62.5"))

    def test_sin_clasificador_usa_ai_probability_percent(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        report = self._report(summary={})
        self.assertEqual(build_ai_estimate(report).percent, Decimal("40.00"))

    def test_clasificador_con_error_cae_a_ai_probability_percent(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        report = self._report(
            summary={"ai_score_breakdown": {"classifier": {"error": True}}}
        )
        self.assertEqual(build_ai_estimate(report).percent, Decimal("40.00"))

    def test_bandas_40_70(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        casos = [
            ("0", "Sin indicios concluyentes"),
            ("39.99", "Sin indicios concluyentes"),
            ("40", "Indicios: revisar con criterio docente"),
            ("69.99", "Indicios: revisar con criterio docente"),
            ("70", "Posible IA: revisar"),
            ("100", "Posible IA: revisar"),
        ]
        for valor, etiqueta in casos:
            report = AnalysisReport(
                ai_probability_percent=Decimal(valor), generated_at=timezone.now()
            )
            self.assertEqual(build_ai_estimate(report).band_label, etiqueta, valor)

    def test_version_detector_y_fecha_salen_del_reporte(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        fecha = timezone.now() - timezone.timedelta(days=90)
        report = AnalysisReport(
            ai_probability_percent=Decimal("10.00"),
            engine_version="vql-antiguo-0.0.1",
            ai_detector="perplexity",
            generated_at=fecha,
        )
        with override_settings(AI_DETECTION_MODE="classifier"):
            estimate = build_ai_estimate(report)
        self.assertEqual(estimate.engine_version, "vql-antiguo-0.0.1")
        self.assertEqual(estimate.ai_detector, "perplexity")
        self.assertEqual(estimate.generated_at, fecha)
        self.assertIn("vql-antiguo-0.0.1", estimate.version_label)

    def test_nota_es_la_de_uso_responsable(self) -> None:
        from apps.reports.ai_estimate import build_ai_estimate

        self.assertEqual(
            build_ai_estimate(self._report(summary={})).note, AI_RESPONSIBLE_USE_NOTE
        )
