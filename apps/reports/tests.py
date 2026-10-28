from __future__ import annotations

from decimal import Decimal
from io import StringIO

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
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
    _HighlightedDocumentPdfBuilder,
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

        response = self.client.get(reverse("reports:detail", args=[self.document.id]))

        self.assertContains(response, "Excluidas: citas, bibliografía")

    def test_legacy_report_does_not_claim_quotes_excluded(self) -> None:
        self.report.exclude_quotes = False
        self.report.save()
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", args=[self.document.id]))

        self.assertContains(response, "Excluidas: bibliografía")
        self.assertNotContains(response, "Excluidas: citas")

    def test_viewer_does_not_paint_similarity_inside_excluded_quote(self) -> None:
        self._quote_inside_similarity_finding()
        self.client.force_login(self.student)

        response = self.client.get(reverse("reports:detail", args=[self.document.id]))
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
