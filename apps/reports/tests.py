from __future__ import annotations

from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User, UserRole
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.core.models import Institution
from apps.documents.models import Document, DocumentKind, DocumentText
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportRiskLevel,
    ReportSource,
    SourceType,
)
from apps.reports.services import (
    build_highlighted_document_pdf,
    resolve_analyzed_content,
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


class DownloadHighlightedDocumentViewTests(ReportsTestCaseMixin, TestCase):
    def test_owner_can_download_the_pdf(self) -> None:
        self.client.force_login(self.student)

        response = self.client.get(
            reverse("reports:download_highlighted", kwargs={"pk": self.document.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_other_students_cannot_download_the_pdf(self) -> None:
        self.client.force_login(self.other_student)

        response = self.client.get(
            reverse("reports:download_highlighted", kwargs={"pk": self.document.id}),
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
