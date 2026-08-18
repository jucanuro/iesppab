from __future__ import annotations

from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.accounts.models import User, UserRole
from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.similarity import InternalSimilarityEngine, SimilarityMatch
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_similarity import (
    WebSimilarityAnalysisResult,
    WebSimilarityMatch,
)
from apps.analysis.models import DocumentFingerprint, DocumentKnowledgeChunk
from apps.analysis.services import DocumentAnalysisService
from apps.core.models import Institution
from apps.documents.models import Document, DocumentKind
from apps.reports.models import AnalysisReport, ReportSource


SAMPLE_TEXT = (
    "El presente trabajo de investigacion analiza la metodologia aplicada "
    "para el estudio de los factores academicos institucionales."
)


class SelfPlagiarismExclusionTests(TestCase):
    """
    Un alumno no debe aparecer marcado por similitud contra su propio
    trabajo anterior (borradores, versiones corregidas, entregas previas).
    """

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

    def _make_document(self, owner: User, title: str) -> Document:
        document = Document(
            institution=self.institution,
            owner=owner,
            uploaded_by=owner,
            title=title,
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(SAMPLE_TEXT),
            sha256_hash=f"hash-{title}",
        )
        document.original_file.save(
            "documento.pdf",
            ContentFile(SAMPLE_TEXT.encode("utf-8")),
            save=False,
        )
        document.save()
        return document

    def _index_chunk(self, document: Document) -> None:
        engine = InternalSimilarityEngine()
        normalized_text = engine._normalize(SAMPLE_TEXT)

        chunk = DocumentKnowledgeChunk.objects.create(
            document=document,
            text_excerpt=SAMPLE_TEXT,
            normalized_text=normalized_text,
            start_offset=0,
            end_offset=len(SAMPLE_TEXT),
            word_count=len(SAMPLE_TEXT.split()),
            content_hash=f"content-hash-{document.id}",
        )

        fingerprints = compute_fingerprints(normalized_text)

        DocumentFingerprint.objects.bulk_create(
            [
                DocumentFingerprint(
                    hash=fingerprint_hash,
                    source_type="internal",
                    source_id=chunk.id,
                )
                for fingerprint_hash in fingerprints
            ]
        )

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_own_previous_document_is_excluded_from_internal_candidates(self) -> None:
        previous_version = self._make_document(self.student, "Entrega version 1")
        current_version = self._make_document(self.student, "Entrega version 2")
        other_students_document = self._make_document(
            self.other_student,
            "Trabajo de otro alumno",
        )

        self._index_chunk(previous_version)
        self._index_chunk(other_students_document)

        service = DocumentAnalysisService(requested_by=self.student)

        candidates, _ = service._get_fingerprint_candidates(
            document=current_version,
            analysis_content=SAMPLE_TEXT,
        )

        candidate_document_ids = {candidate.document_id for candidate in candidates}

        self.assertNotIn(previous_version.id, candidate_document_ids)
        self.assertIn(other_students_document.id, candidate_document_ids)


class ReportSourceDeduplicationTests(TestCase):
    """
    Fragmentos solapados del documento actual pueden compartir la misma
    fuente como mejor candidato; no debe aparecer duplicada en la lista
    de fuentes del reporte.
    """

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
            title="Proyecto prueba",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(SAMPLE_TEXT),
            sha256_hash="hash-proyecto-prueba",
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(SAMPLE_TEXT.encode("utf-8")),
            save=False,
        )
        self.document.save()
        self.report = AnalysisReport.objects.create(document=self.document)
        self.service = DocumentAnalysisService(requested_by=self.student)

    def test_internal_matches_from_same_source_produce_one_report_source(self) -> None:
        same_source_id = self.document.id  # cualquier UUID sirve como fuente

        matches = [
            SimilarityMatch(
                source_document_id=same_source_id,
                source_title="Tesis UCV",
                source_owner_name="UCV",
                matched_percent=Decimal("42.50"),
                text_excerpt="fragmento 1",
                source_excerpt="excerpt 1",
                start_offset=0,
                end_offset=100,
            ),
            SimilarityMatch(
                source_document_id=same_source_id,
                source_title="Tesis UCV",
                source_owner_name="UCV",
                matched_percent=Decimal("42.50"),
                text_excerpt="fragmento 2",
                source_excerpt="excerpt 1",
                start_offset=45,
                end_offset=145,
            ),
        ]

        self.service._replace_internal_similarity_findings(
            report=self.report,
            matches=matches,
            oai_records_by_id={},
        )

        sources = list(ReportSource.objects.filter(report=self.report))
        self.assertEqual(len(sources), 1)
        self.assertEqual(self.report.findings.count(), 2)

    def test_web_matches_from_same_url_produce_one_report_source(self) -> None:
        matches = [
            WebSimilarityMatch(
                title="Repositorio UCV",
                url="https://repositorio.ucv.edu.pe/tesis-1",
                domain="repositorio.ucv.edu.pe",
                matched_percent=Decimal("30.00"),
                text_excerpt="fragmento 1",
                source_excerpt="excerpt 1",
                start_offset=0,
                end_offset=100,
            ),
            WebSimilarityMatch(
                title="Repositorio UCV",
                url="https://repositorio.ucv.edu.pe/tesis-1",
                domain="repositorio.ucv.edu.pe",
                matched_percent=Decimal("31.00"),
                text_excerpt="fragmento 2",
                source_excerpt="excerpt 1",
                start_offset=45,
                end_offset=145,
            ),
        ]

        web_result = WebSimilarityAnalysisResult(
            web_similarity_percent=Decimal("10.00"),
            matches=matches,
        )

        self.service._replace_web_similarity_findings(
            report=self.report,
            web_result=web_result,
        )

        sources = list(ReportSource.objects.filter(report=self.report))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].matched_percent, Decimal("31.00"))
        self.assertEqual(self.report.findings.count(), 2)


class AcademicTextFilterIndexTests(TestCase):
    """
    Un índice de tablas/figuras no debe colarse como texto redactado hacia
    el detector de IA, incluso cuando no tiene el título exacto "índice"
    o cuando cada entrada quedó como su propio párrafo tras la extracción.
    """

    def test_removes_table_and_figure_index_entries_across_paragraphs(self) -> None:
        body_paragraph = (
            "El presente estudio analiza la relación entre las variables "
            "seleccionadas mediante un diseño no experimental de corte "
            "transversal aplicado a la muestra descrita en la metodología."
        )

        index_entries = "\n\n".join(
            [
                "Índice de tablas",
                "Tabla 1. Distribución de la muestra por sexo .......... 45",
                "Tabla 2. Distribución de la muestra por edad .......... 46",
                "Tabla 3. Nivel de confiabilidad del instrumento ....... 47",
                "Tabla 4. Resultados de la prueba de hipótesis ......... 48",
                "Tabla 5. Resumen de correlaciones encontradas ......... 49",
            ]
        )

        content = f"{body_paragraph}\n\n{index_entries}\n\n{body_paragraph}"

        result = AcademicTextFilter().filter_for_similarity(content=content)

        self.assertNotIn("Tabla 1.", result.content)
        self.assertNotIn("Tabla 5.", result.content)
        self.assertIn("índice de tablas/figuras", result.excluded_sections)
        self.assertEqual(result.content.count(body_paragraph), 2)

    def test_keeps_normal_wrapped_prose_paragraph(self) -> None:
        wrapped_prose = "\n".join(
            [
                "El presente estudio analiza la relación entre las",
                "variables seleccionadas mediante un diseño no",
                "experimental de corte transversal aplicado a una",
                "muestra representativa de estudiantes matriculados",
                "durante el periodo académico evaluado en total 120",
            ]
        )

        result = AcademicTextFilter().filter_for_similarity(content=wrapped_prose)

        self.assertIn("variables seleccionadas", result.content)
        self.assertNotIn("índice de tablas/figuras", result.excluded_sections)
