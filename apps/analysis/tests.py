from __future__ import annotations

import importlib.util
import itertools
import random
import re
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock, skipUnless
from uuid import uuid4

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, UserRole
from apps.analysis.common_text import CommonTextFilter
from apps.analysis.engines import ai_segments
from apps.analysis.engines import perplexity_detector as perplexity_module
from apps.analysis.engines.exclusions import calculate_similarity_percent
from apps.analysis.engines.perplexity_detector import PerplexityAnalysisResult
from apps.analysis.engines.fingerprint import (
    compute_fingerprint_positions,
    compute_fingerprints,
)
from apps.analysis.engines.oai_fulltext import (
    FetchedResponse,
    FullTextFetchError,
    OaiFullTextHarvester,
    PoliteClient,
    RepositoryThrottled,
    ResponseTooLarge,
    RobotsUnavailable,
)
from apps.analysis.engines.oai_harvester import OaiRepositoryConfig
from apps.analysis.engines.span_similarity import (
    MATCH_TYPE_EXACT,
    MATCH_TYPE_FUZZY,
    SpanSimilarityEngine,
    SpanSource,
)
from apps.analysis.engines.similarity import (
    CandidateKnowledgeChunk,
    InternalSimilarityEngine,
    SimilarityMatch,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_search import (
    WebCandidateCollector,
    WebPageContent,
    WebSearchResult,
)
from apps.analysis.engines.web_similarity import (
    WebSimilarityAnalysisResult,
    WebSimilarityMatch,
)
from apps.analysis.indexers import DocumentKnowledgeIndexer, OaiFullTextIndexer
from apps.analysis.management.commands.benchmark_similarity import (
    Command as BenchmarkCommand,
)
from apps.analysis.models import (
    SOURCE_TYPE_OAI_FULLTEXT,
    AnalysisJob,
    AnalysisJobStatus,
    DocumentFingerprint,
    DocumentKnowledgeChunk,
    OaiFullText,
    OaiFullTextChunk,
    OaiFullTextStatus,
    OaiRecord,
)
from apps.analysis.retrieval import CandidateRetriever, compute_query_fingerprints
from apps.analysis.services import (
    MAX_ANALYSIS_DELIVERIES,
    DocumentAnalysisError,
    DocumentAnalysisService,
)
from apps.analysis.tasks import run_document_analysis
from apps.core.models import Institution
from apps.documents.extractors import DocumentTextExtractor
from apps.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentText,
)
from apps.documents.tests import DocxDocumentTestMixin
from apps.reports.models import AnalysisReport, ReportSource


SAMPLE_TEXT = (
    "El presente trabajo de investigacion analiza la metodologia aplicada "
    "para el estudio de los factores academicos institucionales."
)


def make_test_document(
    institution: Institution,
    owner: User,
    title: str = "Documento de prueba",
    status: str = DocumentStatus.UPLOADED,
) -> Document:
    document = Document(
        institution=institution,
        owner=owner,
        uploaded_by=owner,
        title=title,
        kind=DocumentKind.OTHER,
        original_filename="documento.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(SAMPLE_TEXT),
        sha256_hash=f"hash-{title}",
        status=status,
    )
    document.original_file.save(
        "documento.pdf",
        ContentFile(SAMPLE_TEXT.encode("utf-8")),
        save=False,
    )
    document.save()
    return document


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


class KnowledgeIndexerFilteredContentTests(TestCase):
    """
    El indexador debe aprender únicamente el texto ya filtrado (el mismo
    que se usa para comparar), no el texto crudo con portada, dedicatoria
    y bibliografía. De lo contrario cada documento contamina el corpus
    interno con boilerplate institucional y genera similitud falsa.
    """

    COVER = (
        "Universidad Nacional Alfonso Barrantes Lingan Facultad de Educacion "
        "Escuela Profesional de Educacion Primaria Tesis para optar el titulo "
        "profesional Autor Juan Perez Asesor Maria Lopez Curso Seminario de "
        "Tesis Cajamarca Peru 2026"
    )
    DEDICATION = "Dedicatoria"
    BODY = (
        "La investigacion analiza la relacion entre el acompanamiento "
        "pedagogico y el desempeno docente en instituciones de educacion "
        "basica regular durante el periodo academico evaluado. Se aplico un "
        "diseno no experimental de corte transversal con un enfoque "
        "cuantitativo sobre una muestra representativa de docentes "
        "seleccionados mediante muestreo probabilistico estratificado. Los "
        "instrumentos fueron validados por juicio de expertos y sometidos a "
        "una prueba piloto para estimar su confiabilidad mediante el "
        "coeficiente alfa de Cronbach. El procesamiento estadistico incluyo "
        "analisis descriptivo e inferencial con pruebas de correlacion para "
        "contrastar las hipotesis planteadas en el estudio. Los hallazgos "
        "evidencian una asociacion positiva y significativa entre las "
        "variables observadas lo cual sugiere que fortalecer el "
        "acompanamiento pedagogico incide favorablemente en la practica "
        "docente cotidiana dentro del aula."
    )
    BIBLIOGRAPHY = (
        "Bibliografia\n"
        "Hernandez R Fernandez C y Baptista P 2014 Metodologia de la "
        "investigacion Mexico McGraw Hill\n"
        "Ministerio de Educacion 2017 Curriculo nacional de la educacion "
        "basica Lima Minedu"
    )

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
        self.raw_content = "\n\n".join(
            [self.COVER, self.DEDICATION, self.BODY, self.BIBLIOGRAPHY]
        )
        document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis de prueba",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(self.raw_content),
            sha256_hash="hash-tesis-de-prueba",
        )
        document.original_file.save(
            "documento.pdf",
            ContentFile(self.raw_content.encode("utf-8")),
            save=False,
        )
        document.save()
        self.document = document
        self.document_text = DocumentText.objects.create(
            document=document,
            content=self.raw_content,
        )

    def _indexed_chunk_text(self) -> str:
        chunks = DocumentKnowledgeChunk.objects.filter(document=self.document)
        return " ".join(chunk.normalized_text for chunk in chunks)

    def test_raw_content_leaks_boilerplate_into_corpus(self) -> None:
        """Comportamiento anterior (regresión a evitar)."""
        DocumentKnowledgeIndexer().index(
            document_text=self.document_text,
            content=self.raw_content,
        )

        indexed = self._indexed_chunk_text()

        self.assertIn("universidad", indexed)
        self.assertIn("dedicatoria", indexed)
        self.assertIn("bibliografia", indexed)

    def test_filtered_content_keeps_boilerplate_out_of_corpus(self) -> None:
        filtered = AcademicTextFilter().filter_for_similarity(
            content=self.raw_content,
        ).content

        DocumentKnowledgeIndexer().index(
            document_text=self.document_text,
            content=filtered,
        )

        indexed = self._indexed_chunk_text()

        self.assertNotIn("universidad", indexed)
        self.assertNotIn("facultad", indexed)
        self.assertNotIn("dedicatoria", indexed)
        self.assertNotIn("bibliografia", indexed)
        self.assertNotIn("mcgraw", indexed)
        self.assertIn("acompanamiento pedagogico", indexed)


class AIDetectorPersonalMarkerTests(TestCase):
    """
    Los marcadores personales deben compararse como palabra/frase completa,
    no como substring. "yo" no debe matchear dentro de "apoyo", "cuyo",
    "ensayo" o "concluyo", que son palabras académicas comunes y activaban
    una reducción de 18 puntos al score de IA sin ninguna razón real.
    """

    def setUp(self) -> None:
        from apps.analysis.engines.ai_detector import SpanishAIDetector

        self.detector = SpanishAIDetector()

    def test_substring_words_do_not_trigger_personal_markers(self) -> None:
        text = (
            "Concluyo que el apoyo institucional, cuyo alcance fue amplio, "
            "permitió un ensayo exitoso"
        )

        ratio = self.detector._phrase_ratio(
            text, self.detector.PERSONAL_MARKERS
        )

        self.assertEqual(ratio, 0.0)

    def test_real_personal_markers_are_detected(self) -> None:
        text = "yo considero que nosotros debemos"

        ratio = self.detector._phrase_ratio(
            text, self.detector.PERSONAL_MARKERS
        )

        self.assertGreater(ratio, 0.0)


class SimilarityTotalCoverageTests(TestCase):
    """
    En una tesis larga con más de MAX_MATCHES fragmentos coincidentes, el %
    total de similitud debe reflejar la cobertura real de TODO el plagio,
    no solo la de los primeros MAX_MATCHES fragmentos. El límite de 35 solo
    debe recortar el detalle mostrado/persistido, nunca el cálculo del score.
    """

    VOCAB = (
        "acompanamiento pedagogico desempeno docente institucion educativa "
        "investigacion cuantitativa diseno transversal muestra representativa "
        "instrumento validado confiabilidad coeficiente correlacion hipotesis "
        "resultado significativo evidencia practica aula estudiante aprendizaje "
        "evaluacion formativa gestion directiva politica curriculo nacional "
        "competencia capacidad estandar rubrica retroalimentacion monitoreo"
    ).split()

    def _self_match_scenario(
        self, engine: InternalSimilarityEngine
    ) -> tuple[str, list[CandidateKnowledgeChunk]]:
        content = " ".join(self.VOCAB * 12)
        current_chunks = engine._build_current_chunks(content=content)
        candidates = [
            CandidateKnowledgeChunk(
                document_id=self.document.id,
                title=f"Fuente {index}",
                owner_name="Repositorio",
                text_excerpt=chunk_text,
                normalized_text=normalized_chunk,
            )
            for index, (chunk_text, normalized_chunk, _, _) in enumerate(
                current_chunks
            )
        ]
        return content, candidates

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
            title="Tesis larga",
            kind=DocumentKind.OTHER,
            original_filename="documento.pdf",
            mime_type="application/pdf",
            file_size_bytes=10,
            sha256_hash="hash-tesis-larga",
        )
        self.document.original_file.save(
            "documento.pdf",
            ContentFile(b"contenido"),
            save=False,
        )
        self.document.save()

    def test_total_similarity_covers_all_matches_not_just_top_n(self) -> None:
        engine = InternalSimilarityEngine()
        engine.MAX_MATCHES = 3

        content, candidates = self._self_match_scenario(engine)

        result = engine.analyze(content=content, candidates=candidates)

        # El detalle se recorta al límite...
        self.assertEqual(len(result.matches), 3)

        # ...pero el % total refleja la cobertura completa (todo el
        # documento es plagio en este escenario).
        self.assertGreaterEqual(result.similarity_percent, Decimal("99.00"))

        # Lo que habría reportado el código anterior: cobertura calculada
        # solo sobre los fragmentos que sobrevivieron al truncado.
        truncated_percent = engine._calculate_total_similarity(
            content_length=len(content),
            matched_ranges=[
                (match.start_offset, match.end_offset)
                for match in result.matches
            ],
        )

        self.assertLess(truncated_percent, Decimal("60.00"))
        self.assertGreater(
            result.similarity_percent - truncated_percent,
            Decimal("30.00"),
        )


class QuoteExclusionSimilarityTests(SimpleTestCase):
    """
    Las citas textuales marcadas como excluidas no suman al % de
    similitud, pero el plagio fuera de comillas sí.
    """

    OWN_BEFORE = " ".join(f"propio{i}" for i in range(80))
    OWN_AFTER = " ".join(f"redaccion{i}" for i in range(80))
    QUOTE = " ".join(f"citado{i}" for i in range(40))
    PLAGIARISM = " ".join(f"copiado{i}" for i in range(40))

    def _candidate(self, text: str) -> CandidateKnowledgeChunk:
        engine = InternalSimilarityEngine()
        return CandidateKnowledgeChunk(
            document_id=uuid4(),
            title="Fuente",
            owner_name="Repositorio",
            text_excerpt=text,
            normalized_text=engine._normalize(text),
        )

    def _analyze(self, content: str, exclude_quotes: bool):
        excluded_ranges = (
            AcademicTextFilter().find_quoted_ranges(content)
            if exclude_quotes
            else []
        )
        return InternalSimilarityEngine().analyze(
            content=content,
            candidates=[self._candidate(f"{self.QUOTE} {self.PLAGIARISM}")],
            excluded_ranges=excluded_ranges,
        )

    def test_copied_quote_does_not_count(self) -> None:
        thesis = f"{self.OWN_BEFORE} “{self.QUOTE}” {self.OWN_AFTER}"

        self.assertGreater(
            self._analyze(thesis, exclude_quotes=False).similarity_percent,
            Decimal("0.00"),
        )
        self.assertEqual(
            self._analyze(thesis, exclude_quotes=True).similarity_percent,
            Decimal("0.00"),
        )

    def test_plagiarism_outside_quotes_still_counts(self) -> None:
        thesis = (
            f"{self.OWN_BEFORE} {self.PLAGIARISM} {self.OWN_AFTER} "
            f"“{self.QUOTE}”."
        )

        result = self._analyze(thesis, exclude_quotes=True)

        self.assertGreater(result.similarity_percent, Decimal("0.00"))
        quote_start = thesis.index("“")
        self.assertTrue(
            all(start < quote_start for start, _ in result.all_matched_ranges)
        )

    def test_unclosed_quote_excludes_nothing(self) -> None:
        thesis = f"{self.OWN_BEFORE} “{self.QUOTE} {self.OWN_AFTER}"

        self.assertEqual(
            self._analyze(thesis, exclude_quotes=True).similarity_percent,
            self._analyze(thesis, exclude_quotes=False).similarity_percent,
        )
        self.assertGreater(
            self._analyze(thesis, exclude_quotes=True).similarity_percent,
            Decimal("0.00"),
        )

    def test_no_artificial_shingle_across_quote_boundary(self) -> None:
        content = (
            f"{self.OWN_BEFORE} uno dos “{self.QUOTE}” tres cuatro "
            f"{self.OWN_AFTER}"
        )
        engine = InternalSimilarityEngine()
        chunks = engine._build_current_chunks(
            content=content,
            excluded_ranges=AcademicTextFilter().find_quoted_ranges(content),
        )

        shingles = set().union(
            *(engine._build_shingles(normalized) for _, normalized, _, _ in chunks)
        )

        # Las palabras a ambos lados de la cita existen, pero nunca juntas.
        self.assertIn("propio78 propio79 uno dos", shingles)
        self.assertIn("tres cuatro redaccion0 redaccion1", shingles)
        self.assertFalse(
            any("dos tres" in shingle for shingle in shingles),
        )
        self.assertFalse(any("citado" in shingle for shingle in shingles))

    def test_without_exclusions_chunks_are_unchanged(self) -> None:
        content = f"{self.OWN_BEFORE} “{self.QUOTE}” {self.OWN_AFTER}"
        engine = InternalSimilarityEngine()

        for chunk_text, normalized, _, _ in engine._build_current_chunks(
            content=content,
        ):
            self.assertEqual(normalized, engine._normalize(chunk_text))

    def test_excluded_chars_leave_numerator_and_denominator(self) -> None:
        # 50 caracteres coinciden, 20 de ellos son cita: cuentan 40 de 80.
        self.assertEqual(
            calculate_similarity_percent(
                content_length=100,
                matched_ranges=[(0, 50)],
                excluded_ranges=[(40, 60)],
            ),
            Decimal("50.00"),
        )


class AcademicTextFilterCoverTests(TestCase):
    """
    Auditoría senior, Hallazgo 3: el filtro de portada borraba prosa
    académica real. En un instituto de formación docente toda introducción
    menciona escuelas, docentes y universidades: eso NO es una carátula.
    """

    BODY = (
        "El presente estudio analiza la relacion entre el acompanamiento "
        "familiar y el rendimiento academico de los estudiantes del nivel "
        "primario en instituciones educativas publicas del distrito. La "
        "investigacion parte de la observacion de que los ninos cuyos padres "
        "participan activamente en las tareas escolares muestran un desempeno "
        "superior en las evaluaciones censales aplicadas por el Ministerio. "
    ) * 12

    def _filter(self, content: str):
        return AcademicTextFilter().filter_for_similarity(content=content)

    def test_real_thesis_intro_mentioning_education_survives(self) -> None:
        """Repro TEST 2 de la auditoría: la introducción real sobrevive."""
        intro = (
            "En el ambito educativo peruano, la escuela rural enfrenta una "
            "tension permanente: el docente unico atiende varios grados a la "
            "vez, la universidad mas cercana que forma a esos maestros queda a "
            "horas de distancia, y el instituto de formacion continua rara vez "
            "llega con acompanamiento real al aula. Esta tesis parte de esa "
            "realidad concreta y no de un ideal abstracto."
        )

        self.assertFalse(AcademicTextFilter()._looks_like_cover(intro))

        result = self._filter(f"{intro}\n\n{self.BODY}")

        self.assertIn(intro, result.content)
        self.assertNotIn("portada", result.excluded_sections)

    def test_wrapped_thesis_intro_mentioning_education_survives(self) -> None:
        intro_wrapped = (
            "En el ambito educativo peruano, la escuela rural enfrenta una\n"
            "tension permanente: el docente unico atiende varios grados a la\n"
            "vez, y la universidad mas cercana que forma a esos maestros y el\n"
            "instituto de formacion continua quedan siempre a horas del aula.\n"
            "Esta tesis parte de esa realidad concreta, no de un ideal."
        )

        self.assertFalse(
            AcademicTextFilter()._looks_like_cover(intro_wrapped)
        )

        result = self._filter(f"{intro_wrapped}\n\n{self.BODY}")

        self.assertIn("instituto de formacion continua", result.content)
        self.assertNotIn("portada", result.excluded_sections)

    def test_real_cover_block_is_removed(self) -> None:
        cover = (
            "UNIVERSIDAD NACIONAL DE EDUCACION\n"
            "FACULTAD DE EDUCACION\n"
            "ESCUELA PROFESIONAL DE EDUCACION PRIMARIA\n"
            "TESIS\n"
            "PARA OPTAR EL TITULO PROFESIONAL DE LICENCIADO\n"
            "AUTOR: Juan Perez\n"
            "ASESOR: Dr. Lopez\n"
            "CURSO: Investigacion\n"
            "LIMA - PERU 2024\n"
        )

        self.assertTrue(AcademicTextFilter()._looks_like_cover(cover))

        result = self._filter(f"{cover}\n\n{self.BODY}")

        self.assertNotIn("UNIVERSIDAD NACIONAL", result.content)
        self.assertIn("acompanamiento familiar", result.content)
        self.assertIn("portada", result.excluded_sections)

    def test_mixed_case_cover_with_fixed_formulas_is_removed(self) -> None:
        cover = (
            "Universidad Nacional de Educacion\n"
            "Facultad de Educacion\n"
            "Tesis para optar el titulo profesional de Licenciado en "
            "Educacion Primaria\n"
            "Presentado por: Juan Perez Quispe\n"
            "Asesor: Dr. Manuel Lopez Rios\n"
            "Lima, Peru - 2024\n"
        )

        self.assertTrue(AcademicTextFilter()._looks_like_cover(cover))

        result = self._filter(f"{cover}\n\n{self.BODY}")

        self.assertNotIn("Presentado por", result.content)
        self.assertIn("acompanamiento familiar", result.content)
        self.assertIn("portada", result.excluded_sections)


class AcademicTextFilterReferenceTailTests(TestCase):
    """
    Auditoría senior, Hallazgo 4: una mención casual de "anexo"/"referencias"
    a mitad del cuerpo truncaba todo lo que seguía. Solo debe cortar en un
    encabezado de sección REAL (línea aislada) cerca del final.
    """

    BODY = (
        "El desarrollo profesional docente en contextos rurales exige "
        "acompanamiento situado y sostenido en el tiempo por parte de los "
        "formadores de la institucion educativa. "
    ) * 40

    def _filter(self, content: str):
        return AcademicTextFilter().filter_for_similarity(content=content)

    def test_casual_anexo_mention_mid_body_does_not_truncate(self) -> None:
        """Repro TEST 8 de la auditoría."""
        first_half = (
            "La gestion del acompanamiento pedagogico en la institucion "
            "educativa se analiza a partir de la evidencia recogida en el "
            "trabajo de campo. "
        ) * 20
        mention = (
            "Los instrumentos completos se presentan en el anexo "
            "correspondiente de este informe. "
        )
        second_half = (
            "El capitulo de discusion contrasta los hallazgos con el marco "
            "teorico y con estudios previos sobre desarrollo profesional "
            "docente en zonas rurales. "
        ) * 20

        doc = f"{first_half}\n\n{mention}\n\n{second_half}"
        result = self._filter(doc)

        self.assertIn("discusion contrasta", result.content)
        self.assertNotIn("bibliografía/anexos", result.excluded_sections)
        # Nada relevante se descartó (solo normalización de espacios).
        self.assertGreaterEqual(len(result.content), len(doc) - 20)

    def test_real_reference_heading_near_end_cuts_the_tail(self) -> None:
        references = "\n\n".join(
            f"Autor {i}, A. ({2000 + i}). Titulo de la obra numero {i}. "
            "Editorial Academica."
            for i in range(1, 15)
        )

        doc = f"{self.BODY}\n\nREFERENCIAS BIBLIOGRAFICAS\n\n{references}"
        result = self._filter(doc)

        self.assertIn("acompanamiento situado", result.content)
        self.assertNotIn("Editorial Academica", result.content)
        self.assertIn("bibliografía/anexos", result.excluded_sections)

    def test_numbered_anexos_heading_near_end_cuts_the_tail(self) -> None:
        doc = (
            f"{self.BODY}\n\nVI. ANEXOS\n\n"
            "Anexo 1: Cuestionario aplicado a docentes.\n\n"
            "Anexo 2: Guia de entrevista semiestructurada."
        )
        result = self._filter(doc)

        self.assertIn("acompanamiento situado", result.content)
        self.assertNotIn("Cuestionario aplicado", result.content)
        self.assertIn("bibliografía/anexos", result.excluded_sections)

    def test_early_reference_heading_does_not_cut(self) -> None:
        doc = f"REFERENCIAS\n\n{self.BODY}"
        result = self._filter(doc)

        self.assertIn("acompanamiento situado", result.content)
        self.assertNotIn("bibliografía/anexos", result.excluded_sections)


class AcademicTextFilterQuotedRangesTests(TestCase):
    """
    Exclusión de citas textuales: solo pares cortos y cerrados. Una
    comilla sin cerrar no debe comerse la prosa que sigue (mismo tipo de
    falla que tuvo "anexo" a mitad del cuerpo).
    """

    def _quoted(self, content: str) -> list[str]:
        return [
            content[start:end]
            for start, end in AcademicTextFilter().find_quoted_ranges(content)
        ]

    def test_typographic_and_angle_quotes_are_detected(self) -> None:
        content = (
            "Segun Freire, “la educacion es un acto de amor”. Para Vygotsky "
            "«el aprendizaje despierta procesos internos» en el nino."
        )

        self.assertEqual(
            self._quoted(content),
            [
                "“la educacion es un acto de amor”",
                "«el aprendizaje despierta procesos internos»",
            ],
        )

    def test_straight_quotes_are_detected(self) -> None:
        content = 'El autor sostiene que "la escuela forma ciudadanos" (p. 12).'

        self.assertEqual(
            self._quoted(content),
            ['"la escuela forma ciudadanos"'],
        )

    def test_unclosed_typographic_quote_excludes_nothing(self) -> None:
        content = (
            "El autor afirma “la educacion transforma la sociedad y luego "
            "el estudiante continua con su propia argumentacion sobre la "
            "practica docente en el aula rural."
        )

        self.assertEqual(self._quoted(content), [])

    def test_unclosed_quote_does_not_pair_with_next_quote(self) -> None:
        content = (
            "Una comilla “huerfana sigue con prosa propia del estudiante y "
            "despues aparece “una cita real” mas adelante."
        )

        self.assertEqual(self._quoted(content), ["“una cita real”"])

    def test_unbalanced_straight_quotes_do_not_swallow_prose(self) -> None:
        # La primera comilla recta no cierra: la siguiente comilla recta es
        # la APERTURA de otra cita (va precedida de espacio), no un cierre.
        content = (
            'Mide 15" de ancho. El estudiante escribe prosa propia y luego '
            'cita "un texto breve" del autor.'
        )

        self.assertEqual(self._quoted(content), ['"un texto breve"'])

    def test_quote_does_not_cross_paragraph_break(self) -> None:
        content = "Inicio “sin cierre en este parrafo.\n\nOtro parrafo” sigue."

        self.assertEqual(self._quoted(content), [])

    def test_quote_with_exactly_max_words_is_excluded(self) -> None:
        words = " ".join(["palabra"] * AcademicTextFilter.QUOTE_MAX_WORDS)
        content = f"Cita: “{words}” fin."

        self.assertEqual(self._quoted(content), [f"“{words}”"])

    def test_quote_longer_than_max_words_excludes_nothing(self) -> None:
        words = " ".join(["palabra"] * (AcademicTextFilter.QUOTE_MAX_WORDS + 1))
        content = f"Cita: “{words}” fin."

        self.assertEqual(self._quoted(content), [])


class AcademicTextFilterBibliographyFlagTests(TestCase):
    BODY = AcademicTextFilterReferenceTailTests.BODY

    def test_bibliography_kept_when_flag_is_off(self) -> None:
        doc = f"{self.BODY}\n\nREFERENCIAS\n\nAutor, A. (2020). Obra. Editorial Academica."

        result = AcademicTextFilter().filter_for_similarity(
            content=doc,
            exclude_bibliography=False,
        )

        self.assertIn("Editorial Academica", result.content)
        self.assertNotIn("bibliografía/anexos", result.excluded_sections)

    def test_reference_word_mid_prose_near_end_does_not_cut(self) -> None:
        doc = (
            f"{self.BODY}\n\nEn las referencias consultadas se observa que el "
            "acompanamiento mejora la practica docente en zonas rurales."
        )

        result = AcademicTextFilter().filter_for_similarity(content=doc)

        self.assertIn("mejora la practica docente", result.content)
        self.assertNotIn("bibliografía/anexos", result.excluded_sections)


class AcademicTextFilterReferenceTailMultipleHeadingsTests(TestCase):
    """
    Bug A de la re-indexación: cuando la cola del documento tiene DOS
    encabezados aislados válidos ("REFERENCIAS BIBLIOGRÁFICAS" seguido más
    adelante de "ANEXOS" — el cierre más común de una tesis), el código
    cortaba en el ÚLTIMO en vez del PRIMERO, dejando toda la bibliografía
    dentro del texto analizado/indexado. Repro real: Proyecto prueba 10
    (REFERENCIAS ~94% del documento, ANEXOS ~98%).
    """

    BODY = (
        "El acompanamiento pedagogico incide en el desempeno docente "
        "dentro del aula durante el periodo evaluado por la institucion "
        "educativa de formacion continua. "
    ) * 30

    CITATIONS = "\n\n".join(
        f"Autor {letter}, A. ({2000 + i}). Titulo de la obra numero {i}. "
        "Universidad Nacional de Ejemplo. Tesis para optar el titulo de "
        "Licenciatura en Educacion."
        for i, letter in enumerate("ABCDEFGHIJ", start=1)
    )

    ANNEXES = (
        "Anexo 1: Cuestionario aplicado a los docentes participantes.\n\n"
        "Anexo 2: Guia de entrevista semiestructurada."
    )

    def _filter(self, content: str):
        return AcademicTextFilter().filter_for_similarity(content=content)

    def test_cuts_at_first_tail_heading_removing_both_references_and_annexes(
        self,
    ) -> None:
        doc = (
            f"{self.BODY}\n\nREFERENCIAS BIBLIOGRÁFICAS\n\n{self.CITATIONS}"
            f"\n\nANEXOS\n\n{self.ANNEXES}"
        )

        result = self._filter(doc)

        self.assertIn("acompanamiento pedagogico", result.content)
        self.assertNotIn("Universidad Nacional de Ejemplo", result.content)
        self.assertNotIn("Tesis para optar el titulo", result.content)
        self.assertNotIn("Cuestionario aplicado", result.content)
        self.assertIn("bibliografía/anexos", result.excluded_sections)
        # Un solo corte: no se cuenta dos veces el mismo excluded_section.
        self.assertEqual(result.excluded_sections.count("bibliografía/anexos"), 1)


class AcademicTextFilterLowValueHeadingWhitespaceTests(TestCase):
    """
    Bug B de la re-indexación: `_is_low_value_paragraph` comparaba
    `lowered.startswith(f"{heading}\\n")`, pero el texto extraído de PDF
    trae con frecuencia un espacio suelto antes del salto de línea
    ("ÍNDICE \\n...") o un número de página huérfano pegado justo antes
    del encabezado ("5 \\nÍNDICE \\n..."), y ambos rompían la comparación
    dejando la tabla de contenido general sin filtrar. Repro real:
    Proyecto prueba 10.
    """

    TOC_ENTRIES = (
        "1.2.1. Justificación legal: ........................ 2\n"
        "2.2.2. Rendimiento Académico: ....................... 6\n"
        "CONCLUSIONES Y SUGERENCIAS ........................... 8"
    )

    def _filter(self, content: str):
        return AcademicTextFilter().filter_for_similarity(content=content)

    def test_trailing_space_before_heading_is_still_detected(self) -> None:
        paragraph = f"ÍNDICE \n{self.TOC_ENTRIES}"

        self.assertTrue(
            AcademicTextFilter()._is_low_value_paragraph(paragraph)
        )

    def test_stray_page_number_before_heading_is_still_detected(self) -> None:
        paragraph = f"5 \nÍNDICE \n{self.TOC_ENTRIES}"

        self.assertTrue(
            AcademicTextFilter()._is_low_value_paragraph(paragraph)
        )

    def test_general_table_of_contents_is_excluded_from_filtered_output(
        self,
    ) -> None:
        body = (
            "El desarrollo profesional docente en contextos rurales "
            "exige acompanamiento situado y sostenido en el tiempo. "
        ) * 10

        doc = f"{body}\n\n5 \nÍNDICE \n{self.TOC_ENTRIES}\n\n{body}"
        result = self._filter(doc)

        self.assertNotIn("Justificación legal", result.content)
        self.assertNotIn("CONCLUSIONES Y SUGERENCIAS", result.content)
        self.assertIn("sección no académica", result.excluded_sections)
        self.assertIn("acompanamiento situado", result.content)


class AcademicTextFilterRunningHeaderTests(TestCase):
    """
    Bug C de la re-indexación (el más grave): un encabezado/pie de página
    ("Instituto de Prueba" / "Alumno Ejemplo <n° página>") repetido en
    cada página del PDF queda pegado dentro de párrafos de prosa real,
    sin párrafo propio que lo aísle — ningún filtro por párrafo puede
    tocarlo. Se detecta por REPETICIÓN a lo largo del documento (no por
    contenido fijo). Repro real: Proyecto prueba 11 (111 repeticiones).

    Riesgo a evitar: no debe arrastrar contenido legítimo que también se
    repite bastante (leyendas de tabla/figura numeradas, filas de totales
    de una encuesta, encabezados de sección numerados) — eso también se
    reprodujo con datos reales al construir este arreglo.
    """

    HEADER_LINE_1 = "Instituto de Prueba Pedagógico"
    HEADER_LINE_2 = "Alumno Ejemplo"
    PROSE_PARAGRAPH = (
        "El acompanamiento pedagogico incide en el desempeno docente "
        "dentro del aula durante el periodo evaluado por la institucion "
        "educativa de formacion continua en la region."
    )

    def _build_document_with_running_header(self, pages: int) -> str:
        parts = []

        for page in range(1, pages + 1):
            parts.append(
                f"{self.HEADER_LINE_1}\n{self.HEADER_LINE_2} {page}"
            )
            parts.append(f"{self.PROSE_PARAGRAPH} Pagina numero {page}.")

        return "\n\n".join(parts)

    def _filter(self, content: str):
        return AcademicTextFilter().filter_for_similarity(content=content)

    def test_repeated_page_header_is_removed_and_prose_survives(self) -> None:
        doc = self._build_document_with_running_header(pages=60)

        result = self._filter(doc)

        self.assertNotIn(self.HEADER_LINE_1, result.content)
        self.assertNotIn(self.HEADER_LINE_2, result.content)
        self.assertIn("encabezado/pie de página repetido", result.excluded_sections)
        # La prosa real de cada "página" sobrevive intacta.
        self.assertIn("Pagina numero 1.", result.content)
        self.assertIn("Pagina numero 60.", result.content)
        self.assertEqual(
            result.content.count("acompanamiento pedagogico"), 60
        )

    def test_low_repeat_count_does_not_remove_anything(self) -> None:
        """
        La misma línea, pero repetida pocas veces (por debajo del umbral
        de página completa) no debe tocarse: podría ser una mención
        legítima puntual, no un header/footer de cada página.
        """
        doc = self._build_document_with_running_header(pages=3)

        result = self._filter(doc)

        self.assertIn(self.HEADER_LINE_1, result.content)
        self.assertNotIn(
            "encabezado/pie de página repetido", result.excluded_sections
        )

    def test_sequentially_numbered_table_captions_are_not_removed(self) -> None:
        """
        Encabezados de tabla numerados ("Tabla Nº 1, 2, 3...") también
        tienen números distintos en cada repetición, igual que una
        paginación real — pero están atados a la cantidad de tablas del
        documento, no a la cantidad de páginas, y no deben confundirse
        con un header/footer aunque no haya ningún header/footer real que
        les gane en repeticiones.
        """
        paragraphs = [
            f"Tabla Nº {n}\nResultados de la dimensión numero {n} del "
            "instrumento aplicado a los docentes participantes."
            for n in range(1, 21)
        ]
        doc = "\n\n".join(paragraphs)

        result = self._filter(doc)

        self.assertIn("Tabla Nº 1", result.content)
        self.assertIn("Tabla Nº 20", result.content)
        self.assertNotIn(
            "encabezado/pie de página repetido", result.excluded_sections
        )

    def test_survey_result_table_template_is_not_removed(self) -> None:
        """
        Repro real (Proyecto prueba 11): un documento con un header/footer
        real, MÁS decenas de tablas de resultados de encuesta que
        comparten las mismas filas fijas ("Frecuencia Porcentaje %",
        "Total 60 100.0"). Esas filas se repiten bastante, pero muchas
        menos veces que el header/footer real — no deben arrastrarse.
        """
        parts = []

        for page in range(1, 70):
            parts.append(
                f"{self.HEADER_LINE_1}\n{self.HEADER_LINE_2} {page}"
            )

            if page % 3 == 0:
                parts.append(
                    "Frecuencia Porcentaje %\nCasi nunca 1 1.7\nTotal 60 100.0"
                )
            else:
                parts.append(f"{self.PROSE_PARAGRAPH} Pagina numero {page}.")

        doc = "\n\n".join(parts)
        result = self._filter(doc)

        self.assertNotIn(self.HEADER_LINE_1, result.content)
        self.assertIn("Frecuencia Porcentaje %", result.content)
        self.assertIn("Total 60 100.0", result.content)


class AnalysisTimeLimitTests(TestCase):
    """
    Un análisis que supera el soft time limit debe quedar FAILED con un
    mensaje claro, no colgado en PROCESSING.
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
        self.document = make_test_document(self.institution, self.student)

    def test_task_has_configured_time_limits(self) -> None:
        self.assertEqual(
            run_document_analysis.soft_time_limit,
            settings.ANALYSIS_TASK_SOFT_TIME_LIMIT,
        )
        self.assertEqual(
            run_document_analysis.time_limit,
            settings.ANALYSIS_TASK_TIME_LIMIT,
        )
        self.assertLess(
            settings.ANALYSIS_TASK_SOFT_TIME_LIMIT,
            settings.ANALYSIS_TASK_TIME_LIMIT,
        )

    def test_soft_time_limit_marks_document_and_job_failed(self) -> None:
        service = DocumentAnalysisService(requested_by=self.student)

        with mock.patch.object(
            service.extractor,
            "extract_and_save",
            side_effect=SoftTimeLimitExceeded(),
        ), self.assertRaises(DocumentAnalysisError):
            service.execute(document_id=self.document.id)

        self.document.refresh_from_db()
        job = AnalysisJob.objects.get(document=self.document)

        self.assertEqual(self.document.status, DocumentStatus.FAILED)
        self.assertIn("tiempo máximo", self.document.error_message)
        self.assertEqual(job.status, AnalysisJobStatus.FAILED)
        self.assertIn("tiempo máximo", job.error_message)
        self.assertIsNotNone(job.finished_at)


@override_settings(
    OAI_HARVEST_ENABLED=False,
    WEB_ANALYSIS_ENABLED=False,
    PERPLEXITY_AI_DETECTION_ENABLED=False,
)
class AnalysisRedeliveryIdempotencyTests(TestCase):
    """
    Con acks_late, una tarea cuyo worker murió se reentrega y vuelve a
    llamar a execute(): no debe duplicar jobs, reportes ni hallazgos.
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
        self.document = make_test_document(self.institution, self.student)
        self.document_text = DocumentText.objects.create(
            document=self.document,
            content=SAMPLE_TEXT * 5,
            word_count=len(SAMPLE_TEXT.split()) * 5,
            character_count=len(SAMPLE_TEXT) * 5,
        )

    def _execute(self, celery_task_id: str):
        service = DocumentAnalysisService(requested_by=self.student)

        with mock.patch.object(
            service.extractor,
            "extract_and_save",
            return_value=self.document_text,
        ), mock.patch.object(
            service.web_candidate_collector,
            "collect",
            return_value=[],
        ):
            return service.execute(
                document_id=self.document.id,
                celery_task_id=celery_task_id,
            )

    def _make_open_job(self, celery_task_id: str, delivery_count: int = 1):
        self.document.status = DocumentStatus.PROCESSING
        self.document.save(update_fields=["status", "updated_at"])

        return AnalysisJob.objects.create(
            document=self.document,
            requested_by=self.student,
            status=AnalysisJobStatus.RUNNING,
            celery_task_id=celery_task_id,
            delivery_count=delivery_count,
        )

    def test_task_is_configured_for_late_ack(self) -> None:
        self.assertTrue(run_document_analysis.acks_late)
        self.assertTrue(run_document_analysis.reject_on_worker_lost)
        self.assertGreater(
            settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"],
            settings.ANALYSIS_TASK_TIME_LIMIT,
        )

    def test_redelivery_reuses_open_job(self) -> None:
        job = self._make_open_job(celery_task_id="tarea-1")

        self._execute(celery_task_id="tarea-1")

        self.assertEqual(AnalysisJob.objects.filter(document=self.document).count(), 1)
        job.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(job.status, AnalysisJobStatus.SUCCESS)
        self.assertEqual(job.delivery_count, 2)
        self.assertEqual(self.document.status, DocumentStatus.COMPLETED)

    def test_repeated_execution_does_not_duplicate_results(self) -> None:
        first_report = self._execute(celery_task_id="tarea-1")
        first_findings = first_report.findings.count()

        second_report = self._execute(celery_task_id="tarea-2")

        self.assertEqual(first_report.id, second_report.id)
        self.assertEqual(
            AnalysisReport.objects.filter(document=self.document).count(),
            1,
        )
        self.assertEqual(second_report.findings.count(), first_findings)
        self.assertEqual(
            DocumentText.objects.filter(document=self.document).count(),
            1,
        )

    def test_too_many_deliveries_marks_failed_without_rerunning(self) -> None:
        job = self._make_open_job(
            celery_task_id="tarea-1",
            delivery_count=MAX_ANALYSIS_DELIVERIES,
        )

        with self.assertRaises(DocumentAnalysisError):
            self._execute(celery_task_id="tarea-1")

        job.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(job.status, AnalysisJobStatus.FAILED)
        self.assertEqual(self.document.status, DocumentStatus.FAILED)
        self.assertIn("inesperada", self.document.error_message)
        self.assertFalse(
            AnalysisReport.objects.filter(document=self.document).exists()
        )

    def test_orphan_job_from_other_task_is_reused_with_fresh_count(self) -> None:
        job = self._make_open_job(
            celery_task_id="tarea-vieja",
            delivery_count=MAX_ANALYSIS_DELIVERIES,
        )

        self._execute(celery_task_id="tarea-nueva")

        job.refresh_from_db()
        self.assertEqual(AnalysisJob.objects.filter(document=self.document).count(), 1)
        self.assertEqual(job.status, AnalysisJobStatus.SUCCESS)
        self.assertEqual(job.delivery_count, 1)
        self.assertEqual(job.celery_task_id, "tarea-nueva")


@override_settings(
    OAI_HARVEST_ENABLED=False,
    WEB_ANALYSIS_ENABLED=False,
    PERPLEXITY_AI_DETECTION_ENABLED=False,
)
class AnalysisExclusionOptionsTests(TestCase):
    """Las opciones de exclusión llegan al reporte y cambian lo analizado."""

    BODY = AcademicTextFilterReferenceTailTests.BODY

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
        self.document = make_test_document(self.institution, self.student)
        content = (
            f"{self.BODY} Segun el autor “la practica reflexiva transforma "
            "la escuela” en contextos rurales.\n\nREFERENCIAS\n\n"
            "Autor, A. (2020). Obra citada. Editorial Academica."
        )
        self.document_text = DocumentText.objects.create(
            document=self.document,
            content=content,
            word_count=len(content.split()),
            character_count=len(content),
        )

    def _execute(self, **options):
        service = DocumentAnalysisService(requested_by=self.student)

        with mock.patch.object(
            service.extractor,
            "extract_and_save",
            return_value=self.document_text,
        ), mock.patch.object(
            service.web_candidate_collector,
            "collect",
            return_value=[],
        ):
            return service.execute(document_id=self.document.id, **options)

    def test_defaults_exclude_quotes_and_bibliography(self) -> None:
        report = self._execute()

        self.assertTrue(report.exclude_quotes)
        self.assertTrue(report.exclude_bibliography)
        self.assertNotIn("Editorial Academica", report.analyzed_content)
        # La cita se marca, no se borra del texto analizado.
        self.assertIn("“la practica reflexiva", report.analyzed_content)
        [[start, end]] = report.summary["excluded_quote_ranges"]
        self.assertEqual(
            report.analyzed_content[start:end],
            "“la practica reflexiva transforma la escuela”",
        )

    def test_options_off_keep_bibliography_and_quotes(self) -> None:
        report = self._execute(exclude_quotes=False, exclude_bibliography=False)

        self.assertFalse(report.exclude_quotes)
        self.assertFalse(report.exclude_bibliography)
        self.assertIn("Editorial Academica", report.analyzed_content)
        self.assertEqual(report.summary["excluded_quote_ranges"], [])

    def test_knowledge_index_never_learns_bibliography(self) -> None:
        self._execute(exclude_bibliography=False)

        indexed = " ".join(
            DocumentKnowledgeChunk.objects.filter(
                document=self.document,
            ).values_list("text_excerpt", flat=True)
        )

        self.assertIn("acompanamiento", indexed)
        self.assertNotIn("Editorial Academica", indexed)


class AnalyzeViewTestMixin:
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
        self.client.force_login(self.student)

    def _post(self, document: Document):
        return self.client.post(reverse("analysis:analyze", args=[document.id]))


class AnalyzeEnqueueTests(AnalyzeViewTestMixin, TestCase):
    """El encolado del análisis es atómico: un doble envío encola una sola vez."""

    @mock.patch("apps.analysis.views.run_document_analysis.delay")
    def test_double_submit_enqueues_once(self, delay: mock.Mock) -> None:
        document = make_test_document(self.institution, self.student)

        self._post(document)
        self._post(document)

        delay.assert_called_once_with(
            str(document.id),
            str(self.student.id),
            exclude_quotes=True,
            exclude_bibliography=True,
        )
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.QUEUED)

    @mock.patch("apps.analysis.views.run_document_analysis.delay")
    def test_unchecked_exclusion_options_are_sent_as_false(
        self,
        delay: mock.Mock,
    ) -> None:
        document = make_test_document(self.institution, self.student)

        self.client.post(
            reverse("analysis:analyze", args=[document.id]),
            {"exclusion_options": "1", "exclude_bibliography": "on"},
        )

        delay.assert_called_once_with(
            str(document.id),
            str(self.student.id),
            exclude_quotes=False,
            exclude_bibliography=True,
        )

    @mock.patch("apps.analysis.views.run_document_analysis.delay")
    def test_document_in_progress_is_not_enqueued(self, delay: mock.Mock) -> None:
        for status in (DocumentStatus.QUEUED, DocumentStatus.PROCESSING):
            document = make_test_document(
                self.institution,
                self.student,
                title=f"Documento {status}",
                status=status,
            )

            self._post(document)

            document.refresh_from_db()
            self.assertEqual(document.status, status)

        delay.assert_not_called()

    @mock.patch(
        "apps.analysis.views.run_document_analysis.delay",
        side_effect=ConnectionError("broker caído"),
    )
    def test_enqueue_failure_does_not_leave_document_queued(
        self,
        delay: mock.Mock,
    ) -> None:
        document = make_test_document(self.institution, self.student)

        self._post(document)

        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.FAILED)
        self.assertIn("No se pudo enviar", document.error_message)


class FailedDocumentReanalysisTests(AnalyzeViewTestMixin, TestCase):
    """Un documento FAILED se puede volver a analizar."""

    @mock.patch("apps.analysis.views.run_document_analysis.delay")
    def test_failed_document_can_be_enqueued_again(self, delay: mock.Mock) -> None:
        document = make_test_document(
            self.institution,
            self.student,
            status=DocumentStatus.FAILED,
        )

        self._post(document)

        delay.assert_called_once()
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.QUEUED)
        self.assertEqual(document.error_message, "")

    def test_failed_document_with_orphan_running_job_shows_reanalyze(self) -> None:
        document = make_test_document(
            self.institution,
            self.student,
            status=DocumentStatus.FAILED,
        )
        AnalysisJob.objects.create(
            document=document,
            requested_by=self.student,
            status=AnalysisJobStatus.RUNNING,
        )

        response = self.client.get(reverse("reports:detail", args=[document.id]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["analysis_in_progress"])


class SweepStaleJobsTests(TestCase):
    """sweep_stale_jobs cierra jobs huérfanos viejos y no toca los recientes."""

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

    def _make_running_job(self, title: str, age: timedelta) -> AnalysisJob:
        document = make_test_document(
            self.institution,
            self.student,
            title=title,
            status=DocumentStatus.PROCESSING,
        )
        job = AnalysisJob.objects.create(
            document=document,
            requested_by=self.student,
            status=AnalysisJobStatus.RUNNING,
        )
        past = timezone.now() - age
        AnalysisJob.objects.filter(pk=job.pk).update(
            created_at=past,
            updated_at=past,
        )
        Document.objects.filter(pk=document.pk).update(updated_at=past)
        return job

    def test_old_job_is_failed_and_recent_job_is_untouched(self) -> None:
        old_job = self._make_running_job("Viejo", age=timedelta(hours=3))
        recent_job = self._make_running_job("Reciente", age=timedelta(minutes=5))

        call_command("sweep_stale_jobs", stdout=StringIO())

        old_job.refresh_from_db()
        recent_job.refresh_from_db()
        old_job.document.refresh_from_db()
        recent_job.document.refresh_from_db()

        self.assertEqual(old_job.status, AnalysisJobStatus.FAILED)
        self.assertEqual(old_job.error_code, "STALE_WORKER_LOST")
        self.assertIsNotNone(old_job.finished_at)
        self.assertEqual(old_job.document.status, DocumentStatus.FAILED)

        self.assertEqual(recent_job.status, AnalysisJobStatus.RUNNING)
        self.assertEqual(recent_job.document.status, DocumentStatus.PROCESSING)

    def test_dry_run_does_not_modify(self) -> None:
        old_job = self._make_running_job("Viejo", age=timedelta(hours=3))

        call_command("sweep_stale_jobs", "--dry-run", stdout=StringIO())

        old_job.refresh_from_db()
        self.assertEqual(old_job.status, AnalysisJobStatus.RUNNING)

    def test_default_threshold_is_greater_than_time_limit(self) -> None:
        self.assertGreater(
            settings.ANALYSIS_STALE_JOB_MINUTES * 60,
            settings.ANALYSIS_TASK_TIME_LIMIT,
        )

    def test_threshold_below_time_limit_is_rejected(self) -> None:
        minutes = settings.ANALYSIS_TASK_TIME_LIMIT // 60

        with self.assertRaises(CommandError):
            call_command(
                "sweep_stale_jobs",
                f"--older-than-minutes={minutes}",
                stdout=StringIO(),
            )

    def test_job_running_just_under_time_limit_is_untouched(self) -> None:
        job = self._make_running_job(
            "Casi al límite",
            age=timedelta(seconds=settings.ANALYSIS_TASK_TIME_LIMIT - 60),
        )

        call_command("sweep_stale_jobs", stdout=StringIO())

        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJobStatus.RUNNING)

    def test_redelivered_job_with_old_created_at_is_untouched(self) -> None:
        job = self._make_running_job("Reentregado", age=timedelta(hours=3))
        AnalysisJob.objects.filter(pk=job.pk).update(updated_at=timezone.now())

        call_command("sweep_stale_jobs", stdout=StringIO())

        job.refresh_from_db()
        self.assertEqual(job.status, AnalysisJobStatus.RUNNING)

    def test_requeued_document_is_not_failed_by_orphan_job(self) -> None:
        job = self._make_running_job("Reencolado", age=timedelta(hours=3))
        Document.objects.filter(pk=job.document_id).update(
            status=DocumentStatus.QUEUED,
            updated_at=timezone.now(),
        )

        call_command("sweep_stale_jobs", stdout=StringIO())

        job.refresh_from_db()
        job.document.refresh_from_db()
        self.assertEqual(job.status, AnalysisJobStatus.FAILED)
        self.assertEqual(job.document.status, DocumentStatus.QUEUED)


class AcademicTextFilterDocxCoverRunTests(TestCase):
    """
    En DOCX cada línea de la carátula llega como su propio párrafo. La
    racha inicial de párrafos cortos se evalúa como un bloque.
    """

    # Portada real de la tesis Mantilla, una línea por párrafo de Word.
    MANTILLA_COVER = [
        "UNIVERSIDAD CÉSAR VALLEJO",
        "FACULTAD DE DERECHO Y HUMANIDADES",
        "ESCUELA PROFESIONAL DE EDUCACIÓN",
        # Título con punto final, como en muchas tesis: no corta la racha.
        "Estrategias lúdicas para mejorar la expresión oral en estudiantes "
        "de educación inicial de una institución educativa de Trujillo, 2023.",
        "TESIS PARA OBTENER EL TÍTULO PROFESIONAL DE LICENCIADA EN EDUCACIÓN INICIAL:",
        "Autora:",
        "Mantilla Rodríguez, Ana Lucía (orcid.org/0000-0002-1234-5678)",
        "Asesor:",
        "Dr. Pérez Gómez, Carlos Alberto (orcid.org/0000-0003-8765-4321)",
        "LÍNEA DE INVESTIGACIÓN:",
        "Didácticas y evaluación de los aprendizajes",
        "TRUJILLO — PERÚ",
        "2023",
    ]
    BODY_PARAGRAPHS = [
        (
            "La expresión oral es una competencia comunicativa que los niños "
            "desarrollan desde los primeros años de escolaridad. En el aula "
            "de educación inicial se observan dificultades para pronunciar, "
            "ordenar ideas y sostener una conversación con sus pares."
        ),
        (
            "El juego constituye una estrategia didáctica pertinente para "
            "estimular el lenguaje. Diversos estudios señalan que las "
            "actividades lúdicas favorecen la participación espontánea y la "
            "confianza de los estudiantes al comunicarse."
        ),
    ]

    def _filter(self, paragraphs: list[str]):
        return AcademicTextFilter().filter_for_similarity(
            content="\n\n".join(paragraphs),
        )

    def test_mantilla_cover_split_in_single_line_paragraphs_is_removed(
        self,
    ) -> None:
        result = self._filter(self.MANTILLA_COVER + self.BODY_PARAGRAPHS)

        for marker in ("Autora:", "Asesor:", "orcid.org", "LÍNEA DE INVESTIGACIÓN", "TRUJILLO", "2023"):
            self.assertNotIn(marker, result.content)

        self.assertIn("portada", result.excluded_sections)
        self.assertIn("La expresión oral es una competencia", result.content)
        self.assertIn("El juego constituye una estrategia", result.content)

    def test_cover_run_stops_at_first_prose_paragraph(self) -> None:
        # Un párrafo corto en mayúsculas DESPUÉS de la prosa no forma parte
        # de la portada aunque se parezca a una línea de carátula.
        trailing_heading = "CAPÍTULO I: AUTORA: MARCO TEÓRICO DE LA INVESTIGACIÓN"
        paragraphs = (
            self.MANTILLA_COVER
            + self.BODY_PARAGRAPHS[:1]
            + [trailing_heading]
            + self.BODY_PARAGRAPHS[1:]
        )

        filter_ = AcademicTextFilter()
        paragraphs_after_split = [p for p in paragraphs if p.strip()]

        self.assertEqual(
            filter_._leading_cover_run_length(paragraphs_after_split),
            len(self.MANTILLA_COVER),
        )
        self.assertIn(trailing_heading, self._filter(paragraphs).content)

    def test_cover_marker_beyond_the_first_paragraphs_is_ignored(self) -> None:
        many_short = [f"Línea corta número {index}" for index in range(30)]

        run_length = AcademicTextFilter()._leading_cover_run_length(
            many_short + ["Autora:"] + self.BODY_PARAGRAPHS,
        )

        # "Autora:" queda fuera de los primeros 25 párrafos, y lo que hay
        # dentro (minúsculas, sin fórmula de carátula) no es portada.
        self.assertEqual(run_length, 0)

    def test_document_starting_with_resumen_is_not_discarded(self) -> None:
        result = self._filter(["RESUMEN"] + self.BODY_PARAGRAPHS)

        self.assertNotIn("portada", result.excluded_sections)
        self.assertIn("La expresión oral es una competencia", result.content)
        self.assertIn("El juego constituye una estrategia", result.content)

    def test_resumen_heading_with_short_keywords_is_not_a_cover(self) -> None:
        paragraphs = [
            "RESUMEN",
            "Palabras clave: juego, expresión oral",
        ] + self.BODY_PARAGRAPHS

        self.assertEqual(
            AcademicTextFilter()._leading_cover_run_length(paragraphs),
            0,
        )
        self.assertIn(
            "La expresión oral es una competencia",
            self._filter(paragraphs).content,
        )


TOPICS = [
    "la comprension lectora",
    "la convivencia escolar",
    "el pensamiento critico",
    "la expresion oral",
    "la motivacion academica",
    "el trabajo colaborativo",
    "la evaluacion formativa",
    "la educacion ambiental",
]


PLACE_SYLLABLES = ["ba", "ce", "di", "fo", "gu", "la", "me", "ni", "po", "ru"]


def _place_name(index: int) -> str:
    """
    Nombre único y sin dígitos por párrafo: las frases de búsqueda web no
    admiten números, y un nombre largo asegura que la frase elegida de
    cada tramo sea distinta y localizable en el texto.
    """
    syllables = "".join(PLACE_SYLLABLES[int(digit)] for digit in f"{index:02d}")
    return f"{syllables.capitalize()}pampacancha"


def _body_paragraph(index: int) -> str:
    topic = TOPICS[index % len(TOPICS)]
    place = _place_name(index)
    return (
        f"En la seccion {index} del estudio se analiza {topic} en estudiantes "
        f"del nivel primario de una institucion publica de {place}. "
        f"Los resultados de la seccion {index} en {place} muestran que {topic} mejora "
        f"cuando el docente organiza actividades planificadas y sostenidas "
        f"durante todo el periodo academico evaluado."
    )


@override_settings(WEB_ANALYSIS_MAX_QUERY_PHRASES=8)
class WebSearchPhrasesFromDocxTests(DocxDocumentTestMixin, TestCase):
    """
    Tesis Mantilla: un DOCX de 113 párrafos daba 0 % de similitud web
    porque llegaba como un solo bloque y solo se buscaba una frase del
    inicio. Las frases deben salir de todo el documento, sin la portada.
    """

    COVER = AcademicTextFilterDocxCoverRunTests.MANTILLA_COVER
    BODY_PARAGRAPH_COUNT = 40

    def _phrases_for_docx(self) -> tuple[list[list[str]], str]:
        paragraphs = self.COVER + [
            _body_paragraph(index) for index in range(self.BODY_PARAGRAPH_COUNT)
        ]
        document = self.make_docx_document(paragraphs)

        extracted = DocumentTextExtractor().extract(document=document)
        filtered = AcademicTextFilter().filter_for_similarity(
            content=extracted.content,
        )

        self.assertIn("portada", filtered.excluded_sections)

        phrase_groups = WebCandidateCollector()._extract_search_phrases(
            content=filtered.content,
        )
        return phrase_groups, filtered.content

    def test_phrases_cover_start_middle_and_end_of_the_document(self) -> None:
        phrase_groups, content = self._phrases_for_docx()
        # Posición relativa (0-1) de la mejor frase de cada tramo.
        positions = [content.find(group[0]) / len(content) for group in phrase_groups]

        self.assertEqual(len(phrase_groups), 8)
        self.assertTrue(all(len(group) == 2 for group in phrase_groups), phrase_groups)
        for group in phrase_groups:
            for phrase in group:
                self.assertIn(phrase, content)
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(any(position < 0.2 for position in positions), positions)
        self.assertTrue(any(0.4 <= position < 0.6 for position in positions), positions)
        self.assertTrue(any(position >= 0.8 for position in positions), positions)

    def test_cover_lines_never_become_search_phrases(self) -> None:
        phrase_groups, _content = self._phrases_for_docx()
        phrases = " ".join(phrase for group in phrase_groups for phrase in group)

        for marker in ("Autora", "Asesor", "orcid", "TRUJILLO", "LÍNEA DE INVESTIGACIÓN"):
            self.assertNotIn(marker, phrases)


class _FakeSearchClient:
    def __init__(self, results_per_query: int = 5) -> None:
        self.queries: list[str] = []
        self.results_per_query = results_per_query

    def search(self, query: str, count: int) -> list[WebSearchResult]:
        self.queries.append(query)
        query_index = len(self.queries)
        return [
            WebSearchResult(
                title=f"Fuente {query_index}-{result_index}",
                url=f"https://example.com/{query_index}/{result_index}",
                domain="example.com",
                snippet="",
            )
            for result_index in range(self.results_per_query)
        ]


class _FakeFetcher:
    def __init__(self, empty_urls: set[str] | None = None) -> None:
        self.empty_urls = empty_urls or set()

    def fetch(self, result: WebSearchResult) -> WebPageContent | None:
        if result.url in self.empty_urls:
            return None

        return WebPageContent(
            title=result.title,
            url=result.url,
            domain=result.domain,
            text="texto de la pagina",
            source_format="HTML",
        )


@override_settings(
    WEB_ANALYSIS_ENABLED=True,
    WEB_ANALYSIS_MAX_QUERY_PHRASES=8,
    WEB_ANALYSIS_MAX_QUERIES=24,
    WEB_ANALYSIS_MAX_PAGES_TOTAL=16,
    WEB_ANALYSIS_MAX_PAGES_PER_QUERY=2,
)
class WebCandidateCollectorQueryTests(SimpleTestCase):
    PHRASE_GROUPS = [
        [f"mejor frase del tramo {index}", f"segunda frase del tramo {index}"]
        for index in range(8)
    ]

    def test_queries_are_built_in_three_rounds_over_every_section(self) -> None:
        queries = WebCandidateCollector()._build_queries(
            phrase_groups=self.PHRASE_GROUPS,
        )

        self.assertEqual(len(queries), 24)
        self.assertEqual(queries[:8], [f'"{group[0]}"' for group in self.PHRASE_GROUPS])
        self.assertEqual(queries[8:16], [f'"{group[1]}"' for group in self.PHRASE_GROUPS])
        # Sin comillas solo va la mejor frase de cada tramo.
        self.assertEqual(queries[16:], [group[0] for group in self.PHRASE_GROUPS])

    def test_no_site_or_filetype_queries(self) -> None:
        queries = WebCandidateCollector()._build_queries(
            phrase_groups=self.PHRASE_GROUPS,
        )

        self.assertFalse(any("site:" in query or "filetype:" in query for query in queries))

    def test_sections_with_a_single_phrase_skip_the_second_round(self) -> None:
        queries = WebCandidateCollector()._build_queries(
            phrase_groups=[["frase uno"], ["frase dos", "frase tres"]],
        )

        self.assertEqual(
            queries,
            ['"frase uno"', '"frase dos"', '"frase tres"', "frase uno", "frase dos"],
        )

    @override_settings(WEB_ANALYSIS_MAX_QUERIES=10)
    def test_total_queries_respect_the_configured_limit(self) -> None:
        queries = WebCandidateCollector()._build_queries(
            phrase_groups=self.PHRASE_GROUPS,
        )

        self.assertEqual(len(queries), 10)
        # El recorte no deja sin buscar el final del documento: la primera
        # ronda recorre todos los tramos antes que cualquier otra.
        self.assertIn(f'"{self.PHRASE_GROUPS[-1][0]}"', queries)

    def _collector(self, **fetcher_kwargs) -> WebCandidateCollector:
        collector = WebCandidateCollector()
        collector.search_client = _FakeSearchClient()
        collector.fetcher = _FakeFetcher(**fetcher_kwargs)
        collector._extract_search_phrases = lambda content: self.PHRASE_GROUPS
        return collector

    def test_pages_are_spread_across_queries_of_the_whole_document(self) -> None:
        collector = self._collector()

        pages = collector.collect(content="irrelevante")

        self.assertEqual(len(pages), 16)
        # Dos páginas por consulta: las 8 consultas con comillas (una por
        # tramo del documento) aportan páginas, no solo las primeras.
        self.assertEqual(len(collector.search_client.queries), 8)
        self.assertEqual(
            collector.search_client.queries,
            [f'"{group[0]}"' for group in self.PHRASE_GROUPS],
        )

    def test_collect_logs_queries_pages_and_pages_with_content(self) -> None:
        collector = self._collector(empty_urls={"https://example.com/1/0"})

        with self.assertLogs("apps.analysis.engines.web_search", level="INFO") as logs:
            pages = collector.collect(content="irrelevante")

        summary = [line for line in logs.output if "Búsqueda web terminada" in line]

        self.assertEqual(len(summary), 1)
        self.assertIn("frases=16", summary[0])
        self.assertIn("consultas=8/24 (límite=24)", summary[0])
        self.assertIn("páginas_consultadas=17", summary[0])
        self.assertIn(f"páginas_con_contenido={len(pages)}", summary[0])


def _synthetic_text(seed: int, words: int) -> str:
    """
    Texto con vocabulario propio por semilla: ningún tramo de 8 palabras
    se repite entre semillas distintas, así el % esperado es exacto.
    """
    rng = random.Random(seed)
    vocabulary = [f"t{seed}p{index}" for index in range(400)]
    return " ".join(rng.choice(vocabulary) for _ in range(words))


def _span_source(text: str, key: str = "fuente", kind: str = "web", **kwargs):
    return SpanSource(key=key, kind=kind, title=key, segments=(text,), **kwargs)


class SpanSimilarityEngineTests(SimpleTestCase):
    def _engine(self, **kwargs):
        return SpanSimilarityEngine(
            min_match_words=kwargs.get("min_match_words", 8),
            max_gap_words=kwargs.get("max_gap_words", 2),
            min_short_run=kwargs.get("min_short_run", 4),
            min_fuzzy_span=kwargs.get("min_fuzzy_span", 15),
            min_fuzzy_ratio=kwargs.get("min_fuzzy_ratio", 0.7),
        )

    def test_literal_copy_is_100_percent(self) -> None:
        text = _synthetic_text(1, 300)

        result = self._engine().analyze(text, [_span_source(text)])

        self.assertEqual(result.similarity_percent, Decimal("100.00"))
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].start_offset, 0)
        self.assertEqual(result.matches[0].end_offset, len(text))

    def test_matches_shorter_than_minimum_are_ignored(self) -> None:
        source = _synthetic_text(1, 100)
        copied = " ".join(source.split()[10:17])  # 7 palabras
        content = f"{_synthetic_text(2, 50)} {copied} {_synthetic_text(3, 43)}"

        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(result.similarity_percent, Decimal("0.00"))

    def test_partial_copy_percent_is_exact_in_words(self) -> None:
        source = _synthetic_text(1, 200)
        copied = " ".join(source.split()[20:70])  # 50 palabras
        content = f"{_synthetic_text(2, 100)} {copied} {_synthetic_text(3, 50)}"

        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(result.countable_word_count, 200)
        self.assertEqual(result.similarity_percent, Decimal("25.00"))
        self.assertEqual(result.matches[0].source_excerpt, copied)

    def test_gaps_of_up_to_two_words_are_merged(self) -> None:
        words = _synthetic_text(1, 40).split()
        changed = list(words)
        changed[20] = "distinta"
        changed[21] = "otra"
        content = " ".join(changed)

        result = self._engine().analyze(content, [_span_source(" ".join(words))])

        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].word_count, 40)
        self.assertEqual(result.similarity_percent, Decimal("100.00"))

    def test_gaps_longer_than_limit_split_the_match(self) -> None:
        words = _synthetic_text(1, 40).split()
        changed = list(words)
        changed[20:23] = ["distinta", "otra", "nueva"]
        content = " ".join(changed)

        result = self._engine().analyze(content, [_span_source(" ".join(words))])

        self.assertEqual(len(result.matches), 2)
        self.assertEqual(result.similarity_percent, Decimal("92.50"))

    def test_each_word_is_attributed_to_one_source_largest_first(self) -> None:
        big = _synthetic_text(1, 100)
        small = " ".join(big.split()[80:100]) + " " + _synthetic_text(2, 30)
        content = f"{big} {_synthetic_text(2, 30)}"

        result = self._engine().analyze(
            content,
            [
                _span_source(small, key="chica"),
                _span_source(big, key="grande"),
            ],
        )

        by_key = {item.source.key: item.word_count for item in result.sources}
        # "grande" se queda con sus 100; "chica" solo con las 30 palabras
        # que nadie más cubría (las 20 compartidas no se cuentan dos veces).
        self.assertEqual(by_key, {"grande": 100, "chica": 30})
        self.assertEqual(result.similarity_percent, Decimal("100.00"))
        self.assertEqual(
            sum(item.percent for item in result.sources),
            result.similarity_percent,
        )

    def test_leftover_shorter_than_minimum_is_not_attributed(self) -> None:
        big = _synthetic_text(1, 100)
        # Comparte las últimas 20 palabras con "grande" y le suma 5 propias.
        tail = _synthetic_text(2, 5)
        small = " ".join(big.split()[80:100]) + " " + tail
        content = f"{big} {tail}"

        result = self._engine().analyze(
            content,
            [_span_source(small, key="chica"), _span_source(big, key="grande")],
        )

        self.assertEqual([item.source.key for item in result.sources], ["grande"])

    def test_excluded_quote_is_not_countable_and_breaks_matches(self) -> None:
        source = _synthetic_text(1, 60)
        words = source.split()
        quoted = " ".join(words[25:35])
        content = (
            " ".join(words[:25]) + ' "' + quoted + '" ' + " ".join(words[35:])
        )
        quote_start = content.index('"')
        quote_end = content.index('"', quote_start + 1) + 1

        result = self._engine().analyze(
            content,
            [_span_source(source)],
            excluded_ranges=[(quote_start, quote_end)],
        )

        self.assertEqual(result.countable_word_count, 50)
        self.assertEqual(result.similarity_percent, Decimal("100.00"))
        self.assertEqual(len(result.matches), 2)

        for match in result.matches:
            self.assertFalse(
                match.start_offset < quote_end and match.end_offset > quote_start
            )

    def test_matches_do_not_cross_source_segments(self) -> None:
        words = _synthetic_text(1, 12).split()
        content = " ".join(words)
        source = SpanSource(
            key="partida",
            kind="internal",
            title="partida",
            segments=(" ".join(words[:6]), " ".join(words[6:])),
        )

        result = self._engine().analyze(content, [source])

        self.assertEqual(result.similarity_percent, Decimal("0.00"))

    def test_accents_and_case_are_normalized(self) -> None:
        source = "La Educación Inicial promueve el desarrollo integral del niño en el aula"
        content = "la educacion inicial promueve el desarrollo integral del nino en el aula"

        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(result.similarity_percent, Decimal("100.00"))


    def _paraphrase(self, text: str, every: int = 6) -> str:
        words = text.split()
        return " ".join(
            f"cambio{index}" if index % every == every - 1 else word
            for index, word in enumerate(words)
        )

    def test_light_paraphrase_is_detected_as_approximate(self) -> None:
        source = _synthetic_text(1, 120)
        content = self._paraphrase(source)

        exact_only = self._engine(min_short_run=8).analyze(
            content, [_span_source(source)]
        )
        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(exact_only.similarity_percent, Decimal("0.00"))
        # 1 de cada 6 palabras cambiada: solo cuentan las 100 idénticas de 120.
        self.assertEqual(result.similarity_percent, Decimal("83.33"))
        self.assertEqual(
            {match.match_type for match in result.matches},
            {MATCH_TYPE_FUZZY},
        )

    def test_approximate_block_highlights_changed_words_but_does_not_count_them(
        self,
    ) -> None:
        source = _synthetic_text(1, 120)
        content = self._paraphrase(source)

        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(len(result.matches), 1)
        match = result.matches[0]
        # El bloque resaltado va de la primera palabra a la penúltima (la
        # última es una cambiada), cambiadas intermedias incluidas.
        self.assertEqual(match.start_offset, 0)
        self.assertTrue(content[: match.end_offset].endswith(source.split()[118]))
        self.assertIn("cambio5", match.text_excerpt)
        self.assertEqual(match.word_count, 100)
        self.assertEqual(result.sources[0].word_count, 100)

    def test_exact_match_stays_exact_with_fuzzy_enabled(self) -> None:
        text = _synthetic_text(1, 100)

        result = self._engine().analyze(text, [_span_source(text)])

        self.assertEqual(result.matches[0].match_type, MATCH_TYPE_EXACT)

    def test_short_runs_below_fuzzy_span_are_ignored(self) -> None:
        source = _synthetic_text(1, 100).split()
        # Dos tramos de 5 palabras con un hueco de 1: 11 < 15 palabras.
        block = source[10:15] + ["cambio"] + source[16:21]
        content = " ".join(
            _synthetic_text(2, 40).split() + block + _synthetic_text(3, 40).split()
        )

        result = self._engine().analyze(content, [_span_source(" ".join(source))])

        self.assertEqual(result.similarity_percent, Decimal("0.00"))

    def test_short_runs_out_of_order_in_source_are_not_chained(self) -> None:
        source = _synthetic_text(1, 200).split()
        # Tramos de 5 palabras idénticos, pero tomados de lugares distantes
        # de la fuente: no es una paráfrasis de un mismo pasaje.
        block = []
        for start in (150, 20, 90, 60):
            block += source[start : start + 5] + ["cambio"]
        content = " ".join(
            _synthetic_text(2, 40).split() + block + _synthetic_text(3, 40).split()
        )

        result = self._engine().analyze(content, [_span_source(" ".join(source))])

        self.assertEqual(result.similarity_percent, Decimal("0.00"))

    def test_fuzzy_ratio_rejects_sparse_blocks(self) -> None:
        source = _synthetic_text(1, 120)
        # 4 idénticas + 2 cambiadas: 67 % < 70 %.
        content = self._paraphrase(source, every=3)

        result = self._engine().analyze(content, [_span_source(source)])

        self.assertEqual(result.similarity_percent, Decimal("0.00"))

class CandidateRetrieverTests(TestCase):
    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba",
        )
        self.student = self._user("alumno1")
        self.other_student = self._user("alumno2")
        self.current = make_test_document(
            self.institution, self.student, title="Documento actual"
        )

    def _user(self, username: str) -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

    def _indexed_document(self, owner: User, title: str, content: str) -> Document:
        document = make_test_document(self.institution, owner, title=title)
        DocumentKnowledgeIndexer().index(
            DocumentText(document=document),
            content=content,
        )
        return document

    def _retriever(self, **kwargs):
        return CandidateRetriever(
            max_document_frequency=kwargs.get("max_document_frequency", 50),
            max_candidates=kwargs.get("max_candidates", 200),
        )

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_long_internal_document_is_retrieved_whole(self) -> None:
        # ~66 fragmentos: con la recuperación vieja (top-40 fragmentos) la
        # copia completa no podía pasar de ~60 %.
        text = _synthetic_text(7, 3000)
        source = self._indexed_document(self.other_student, "Tesis larga", text)

        result = self._retriever().retrieve(self.current, text)

        self.assertEqual([item.source_id for item in result.sources], [source.id])
        self.assertGreater(len(result.sources[0].chunks), 40)

        span = SpanSimilarityEngine(min_match_words=8, max_gap_words=2).analyze(
            text,
            result.to_span_sources(),
        )
        self.assertEqual(span.similarity_percent, Decimal("100.00"))

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_common_fingerprints_do_not_select_candidates(self) -> None:
        common = _synthetic_text(8, 60)

        # Tres documentos idénticos: toda huella suya está en las tres
        # fuentes (frecuencia 3), por encima del umbral.
        for index in range(3):
            self._indexed_document(
                self.other_student,
                f"Comparte frase común {index}",
                common,
            )

        result = self._retriever(max_document_frequency=2).retrieve(
            self.current,
            f"{common} {_synthetic_text(30, 60)}",
        )

        self.assertEqual(result.sources, [])

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_filters_apply_before_the_candidate_cut(self) -> None:
        text = _synthetic_text(9, 200)
        # El trabajo previo del propio alumno comparte TODO el texto y
        # encabezaría el ranking; igual no debe ocupar el único cupo.
        self._indexed_document(self.student, "Mi versión anterior", text)
        other = self._indexed_document(
            self.other_student,
            "Trabajo de otro",
            " ".join(text.split()[:80]) + " " + _synthetic_text(10, 120),
        )

        result = self._retriever(max_candidates=1).retrieve(self.current, text)

        self.assertEqual([item.source_id for item in result.sources], [other.id])

    def test_query_fingerprints_do_not_cross_excluded_quotes(self) -> None:
        engine = InternalSimilarityEngine()
        left = "uno dos tres cuatro cinco seis"
        right = "siete ocho nueve diez once doce"
        content = f'{left} "cita" {right}'
        quote_start = content.index('"')
        quote_end = content.rindex('"') + 1

        fingerprints = compute_query_fingerprints(
            content=content,
            excluded_ranges=[(quote_start, quote_end)],
            normalize=engine._normalize,
        )

        expected = compute_fingerprints(left) | compute_fingerprints(right)
        self.assertEqual(fingerprints, expected)


@override_settings(OAI_HARVEST_ENABLED=False, WEB_ANALYSIS_ENABLED=False)
class SpanCommonTextRemovalTests(SimpleTestCase):
    def test_common_run_is_removed_and_kept_for_the_report(self) -> None:
        template = _synthetic_text(41, 20)
        copied = _synthetic_text(42, 20)
        content = f"{template} {_synthetic_text(43, 20)} {copied}"
        engine = SpanSimilarityEngine(min_match_words=8, max_gap_words=2)
        alignment = engine.align(content, [_span_source(f"{template} x {copied}")])

        engine.remove_common_text(alignment, lambda first, last: first == 0)
        result = engine.attribute(content, alignment)

        self.assertEqual(result.similarity_percent, Decimal("33.33"))
        self.assertEqual(alignment.common_runs, [(0, 19)])
        self.assertEqual(
            engine.alignment_to_json(alignment)["common_text"],
            [[0, 19, 0, len(template)]],
        )

    def test_source_with_only_common_text_leaves_the_alignment(self) -> None:
        template = _synthetic_text(41, 20)
        content = f"{template} {_synthetic_text(43, 20)}"
        engine = SpanSimilarityEngine(min_match_words=8, max_gap_words=2)
        alignment = engine.align(content, [_span_source(template)])

        engine.remove_common_text(alignment, lambda first, last: True)
        result = engine.attribute(content, alignment)

        self.assertEqual(alignment.sources, [])
        self.assertEqual(result.similarity_percent, Decimal("0.00"))
        self.assertEqual(result.sources, [])

    def test_other_source_still_counts_the_words_in_a_longer_run(self) -> None:
        template = _synthetic_text(41, 20)
        passage = f"{_synthetic_text(42, 80)} {template}"
        content = f"{passage} {_synthetic_text(43, 100)}"
        engine = SpanSimilarityEngine(min_match_words=8, max_gap_words=2)
        alignment = engine.align(
            content,
            [
                _span_source(template, key="plantilla"),
                _span_source(passage, key="copia"),
            ],
        )

        engine.remove_common_text(alignment, lambda first, last: first == 80)
        result = engine.attribute(content, alignment)

        self.assertEqual(result.similarity_percent, Decimal("50.00"))
        self.assertEqual([item.source.key for item in result.sources], ["copia"])

    def _gap_alignment(self):
        self.left = _synthetic_text(41, 20)
        self.right = _synthetic_text(42, 20)
        self.gap_content = f"{self.left} en los {self.right} {_synthetic_text(43, 58)}"
        engine = SpanSimilarityEngine(min_match_words=8, max_gap_words=2)
        alignment = engine.align(
            self.gap_content,
            [_span_source(f"{self.left} sin hueco {self.right}")],
        )

        return engine, alignment

    def test_runs_joined_by_a_gap_are_evaluated_apart(self) -> None:
        engine, alignment = self._gap_alignment()

        self.assertEqual(engine.coverage_runs(alignment.sources[0]), [(0, 41)])
        self.assertEqual(
            engine.common_text_runs(alignment.sources[0]),
            [(0, 19), (22, 41)],
        )

    def test_gap_between_two_common_runs_is_common(self) -> None:
        engine, alignment = self._gap_alignment()

        engine.remove_common_text(alignment, lambda first, last: True)
        result = engine.attribute(self.gap_content, alignment)

        self.assertEqual(alignment.common_runs, [(0, 41)])
        self.assertEqual(result.similarity_percent, Decimal("0.00"))

    def test_gap_next_to_one_common_run_leaves_without_counting(self) -> None:
        engine, alignment = self._gap_alignment()

        engine.remove_common_text(alignment, lambda first, last: first == 0)
        result = engine.attribute(self.gap_content, alignment)

        self.assertEqual(alignment.common_runs, [(0, 19)])
        self.assertEqual(engine.coverage_runs(alignment.sources[0]), [(22, 41)])
        self.assertEqual(result.similarity_percent, Decimal("20.00"))

    def test_gap_between_runs_that_are_not_common_stays(self) -> None:
        engine, alignment = self._gap_alignment()

        engine.remove_common_text(alignment, lambda first, last: False)
        result = engine.attribute(self.gap_content, alignment)

        self.assertEqual(alignment.common_runs, [])
        self.assertEqual(result.similarity_percent, Decimal("42.00"))

    def test_paraphrase_block_is_evaluated_whole(self) -> None:
        words = _synthetic_text(44, 28).split()
        changed = list(words)

        for index in range(5, 28, 6):
            changed[index] = "cambiada"

        content = f"{' '.join(changed)} {_synthetic_text(45, 72)}"
        engine = SpanSimilarityEngine(
            min_match_words=8,
            max_gap_words=2,
            min_short_run=4,
            min_fuzzy_span=15,
            min_fuzzy_ratio=0.7,
        )
        alignment = engine.align(content, [_span_source(" ".join(words))])
        aligned = alignment.sources[0]

        self.assertTrue(aligned.fuzzy_words)
        self.assertEqual(
            engine.common_text_runs(aligned),
            engine.coverage_runs(aligned),
        )


class CommonTextFilterTests(TestCase):
    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba",
        )
        self.student = self._user("alumno1")
        self.current = make_test_document(
            self.institution,
            self.student,
            title="Documento actual",
        )
        self.template = _synthetic_text(51, 40)
        self.original = _synthetic_text(52, 60)
        self.content = f"{self.template} {self.original}"

    def _user(self, username: str) -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

    def _records_with(self, count: int, text: str, first_seed: int = 100) -> list:
        records = []

        for offset in range(count):
            seed = first_seed + offset
            record = _oai_record(seed)
            OaiFullTextIndexer().index(
                record,
                content=f"{_synthetic_text(seed, 100)} {text}",
            )
            records.append(record)

        return records

    def _internal_with(self, owner: User, text: str, title: str) -> None:
        DocumentKnowledgeIndexer().index(
            DocumentText(
                document=make_test_document(self.institution, owner, title=title)
            ),
            content=f"{text} {_synthetic_text(sum(map(ord, title)), 100)}",
        )

    def _run(self, min_documents: int):
        with override_settings(SIMILARITY_COMMON_TEXT_MIN_DOCS=min_documents):
            service = DocumentAnalysisService(requested_by=self.student)

        return service._run_similarity(
            document=self.current,
            analysis_content=self.content,
            quoted_ranges=[],
            collect_web_pages=list,
            engine="span",
        )

    def test_text_shared_by_enough_documents_does_not_count(self) -> None:
        self._records_with(3, self.template)

        result = self._run(min_documents=3)

        self.assertEqual(result.total_similarity_percent, Decimal("0.00"))
        self.assertEqual(result.span_result.sources, [])
        self.assertEqual(
            result.span_alignment["common_text"],
            [[0, 39, 0, len(self.template)]],
        )

    def test_text_shared_by_too_few_documents_counts(self) -> None:
        self._records_with(2, self.template)

        result = self._run(min_documents=3)

        self.assertEqual(result.total_similarity_percent, Decimal("40.00"))
        self.assertEqual(result.span_alignment["common_text"], [])

    def test_zero_turns_the_filter_off(self) -> None:
        self._records_with(3, self.template)

        result = self._run(min_documents=0)

        self.assertEqual(result.total_similarity_percent, Decimal("40.00"))
        self.assertEqual(result.span_alignment["common_text"], [])

    def test_frequent_fingerprints_spread_over_documents_are_not_common(self) -> None:
        # Cada huella está en 3 documentos, pero solo la fuente tiene el
        # tramo entero: es lo que le pasa al título de una tesis, hecho de
        # 4-gramas frecuentes. Los trozos sí son comunes en sus fuentes, pero
        # el tramo entero sigue contando para la fuente que lo tiene.
        words = self.template.split()
        whole = self._records_with(1, self.template)[0]

        for index, start in enumerate((0, 13, 26)):
            piece = " ".join(words[start : start + 14])
            self._records_with(2, piece, first_seed=200 + 10 * index)

        result = self._run(min_documents=3)

        self.assertEqual(result.total_similarity_percent, Decimal("40.00"))
        self.assertEqual(
            [item.source.source_id for item in result.span_result.sources],
            [whole.id],
        )

    def _run_on(self, content: str, min_documents: int):
        self.content = content

        return self._run(min_documents=min_documents)

    def test_two_formulas_joined_by_a_gap_are_common(self) -> None:
        # Tesis JASMIN: "la competencia resuelve problemas [...] en los niños
        # y niñas de 4 años": ninguna fuente repite las dos fórmulas juntas,
        # pero cada una está en muchas.
        left = _synthetic_text(61, 20)
        right = _synthetic_text(62, 20)
        self._records_with(3, left, first_seed=300)
        self._records_with(3, right, first_seed=310)
        self._records_with(1, f"{left} sin hueco {right}", first_seed=320)

        result = self._run_on(
            f"{left} en los {right} {_synthetic_text(63, 58)}",
            min_documents=3,
        )

        self.assertEqual(result.total_similarity_percent, Decimal("0.00"))
        self.assertEqual(
            [run[:2] for run in result.span_alignment["common_text"]],
            [[0, 41]],
        )

    def test_passage_with_one_common_side_keeps_the_other_side(self) -> None:
        left = _synthetic_text(61, 20)
        right = _synthetic_text(62, 20)
        self._records_with(3, left, first_seed=300)
        source = self._records_with(1, f"{left} sin hueco {right}", first_seed=320)[0]

        result = self._run_on(
            f"{left} en los {right} {_synthetic_text(63, 58)}",
            min_documents=3,
        )

        self.assertEqual(result.total_similarity_percent, Decimal("20.00"))
        self.assertEqual(
            [item.source.source_id for item in result.span_result.sources],
            [source.id],
        )

    def test_copied_passage_with_a_changed_word_still_counts(self) -> None:
        left = _synthetic_text(61, 20)
        right = _synthetic_text(62, 20)
        self._records_with(1, f"{left} sin hueco {right}", first_seed=320)

        result = self._run_on(
            f"{left} en los {right} {_synthetic_text(63, 58)}",
            min_documents=3,
        )

        self.assertEqual(result.total_similarity_percent, Decimal("42.00"))
        self.assertEqual(result.span_alignment["common_text"], [])

    @override_settings()
    def test_default_is_five_documents(self) -> None:
        del settings.SIMILARITY_COMMON_TEXT_MIN_DOCS

        self.assertEqual(CommonTextFilter().min_documents, 5)

    def test_ratio_allows_a_variation_inside_the_run(self) -> None:
        # Los demás documentos repiten el tramo con el final cambiado.
        words = self.template.split()
        self._records_with(1, self.template)
        self._records_with(2, " ".join(words[:32]), first_seed=120)

        result = self._run(min_documents=3)

        self.assertEqual(result.total_similarity_percent, Decimal("0.00"))

    def test_versions_of_one_owner_count_as_one_document(self) -> None:
        other = self._user("alumno2")

        for index in range(3):
            self._internal_with(other, self.template, f"Versión {index}")

        self.assertEqual(
            self._run(min_documents=2).total_similarity_percent,
            Decimal("40.00"),
        )

        self._internal_with(self._user("alumno3"), self.template, "Otra tesis")

        self.assertEqual(
            self._run(min_documents=2).total_similarity_percent,
            Decimal("0.00"),
        )

    def test_own_documents_do_not_count(self) -> None:
        self._records_with(1, self.template)
        self._internal_with(self.student, self.template, "Versión anterior")

        self.assertEqual(
            self._run(min_documents=2).total_similarity_percent,
            Decimal("40.00"),
        )

    def test_summary_and_full_text_of_a_record_are_one_document(self) -> None:
        record = self._records_with(1, self.template)[0]
        block_ids = record.full_text_chunks.values_list("id", flat=True)
        DocumentFingerprint.objects.bulk_create(
            DocumentFingerprint(hash=value, source_type="oai", source_id=record.id)
            for value in DocumentFingerprint.objects.filter(
                source_id__in=block_ids,
            ).values_list("hash", flat=True)
        )

        self.assertEqual(
            self._run(min_documents=2).total_similarity_percent,
            Decimal("40.00"),
        )

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_oai_does_not_count_when_it_is_disabled(self) -> None:
        self._records_with(3, self.template)
        other = self._user("alumno2")
        self._internal_with(other, self.template, "Tesis interna")

        self.assertEqual(
            self._run(min_documents=2).total_similarity_percent,
            Decimal("40.00"),
        )


class SpanEngineServiceTests(TestCase):
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
        self.current = make_test_document(self.institution, self.student)
        self.internal_text = _synthetic_text(11, 300)
        self.web_text = _synthetic_text(12, 300)
        self.source = make_test_document(
            self.institution, self.other_student, title="Fuente interna"
        )
        DocumentKnowledgeIndexer().index(
            DocumentText(document=self.source),
            content=self.internal_text,
        )
        self.web_page = WebPageContent(
            title="Página",
            url="https://example.com/pagina",
            domain="example.com",
            text=self.web_text,
            source_format="html",
        )
        # 100 palabras de la fuente interna + 50 de la web + 50 originales.
        self.content = " ".join(
            [
                " ".join(self.internal_text.split()[:100]),
                " ".join(self.web_text.split()[:50]),
                _synthetic_text(13, 50),
            ]
        )
        self.service = DocumentAnalysisService(requested_by=self.student)

    def _run(self, engine: str, retrieval: str = "vieja"):
        return self.service._run_similarity(
            document=self.current,
            analysis_content=self.content,
            quoted_ranges=[],
            collect_web_pages=lambda: [self.web_page],
            engine=engine,
            retrieval=retrieval,
        )

    def test_span_internal_plus_web_equals_total(self) -> None:
        result = self._run("span")

        self.assertEqual(result.retrieval, "nueva")
        self.assertEqual(result.internal_result.similarity_percent, Decimal("50.00"))
        self.assertEqual(result.web_result.web_similarity_percent, Decimal("25.00"))
        self.assertEqual(result.total_similarity_percent, Decimal("75.00"))

    def test_span_findings_persist_with_source_percent(self) -> None:
        result = self._run("span")
        report = AnalysisReport.objects.create(document=self.current)

        self.service._replace_internal_similarity_findings(
            report=report,
            matches=result.internal_result.matches,
            oai_records_by_id=result.oai_records_by_id,
            algorithm=result.internal_algorithm,
        )
        self.service._replace_web_similarity_findings(
            report=report,
            web_result=result.web_result,
            algorithm=result.web_algorithm,
        )

        percents = {
            source.title: source.matched_percent
            for source in ReportSource.objects.filter(report=report)
        }
        self.assertEqual(
            percents,
            {"Fuente interna": Decimal("50.00"), "Página": Decimal("25.00")},
        )
        self.assertEqual(
            set(report.findings.values_list("metadata__algorithm", flat=True)),
            {"span-exact-words-v1", "brave-search-span-exact-words-v1"},
        )

    def test_span_findings_mark_paraphrase_as_approximate(self) -> None:
        words = self.web_text.split()[:60]
        paraphrased = " ".join(
            "cambio" if index % 6 == 5 else word for index, word in enumerate(words)
        )
        self.content = f"{_synthetic_text(13, 60)} {paraphrased}"
        result = self._run("span")
        report = AnalysisReport.objects.create(document=self.current)

        self.service._replace_web_similarity_findings(
            report=report,
            web_result=result.web_result,
            algorithm=result.web_algorithm,
        )

        # 50 palabras idénticas de 120 contables: las cambiadas no suman.
        self.assertEqual(
            result.web_result.web_similarity_percent, Decimal("41.67")
        )
        self.assertEqual(
            set(
                report.findings.values_list(
                    "metadata__match_type", "metadata__match_type_label"
                )
            ),
            {("aproximada", "paráfrasis/coincidencia aproximada")},
        )

    def test_chunk_engine_accepts_both_retrievals(self) -> None:
        for retrieval in ("vieja", "nueva"):
            result = self._run("chunk", retrieval)

            self.assertEqual(result.retrieval, retrieval)
            self.assertGreater(result.total_similarity_percent, Decimal("0.00"))

    @override_settings(SIMILARITY_ENGINE="chunk", SIMILARITY_RETRIEVAL="vieja")
    def test_default_configuration_keeps_the_current_engine(self) -> None:
        result = self.service._run_similarity(
            document=self.current,
            analysis_content=self.content,
            quoted_ranges=[],
            collect_web_pages=lambda: [],
        )

        self.assertEqual((result.engine, result.retrieval), ("chunk", "vieja"))
        self.assertEqual(result.internal_algorithm, "knowledge-chunks-shingling")


class BenchmarkSimilarityCommandTests(TestCase):
    def test_span_engine_meets_acceptance_criteria(self) -> None:
        command = BenchmarkCommand(stdout=StringIO())
        call_command(command, "--no-csv")

        for case in command.cases:
            result = command.results[(case.name, "span")]
            verdict = command._passes(case, result)

            self.assertIn(verdict, {"OK", "-"}, f"{case.name}: {result.percent}%")

            if case.category == "sintetico":
                self.assertEqual(result.detected_sources, case.expected_sources)

        # Nada de lo indexado por el banco de pruebas queda en la base.
        self.assertFalse(Institution.objects.filter(slug__startswith="benchmark-").exists())


# --- Detección de IA por segmentos (AI_DETECTION_MODE=segments) ---
#
# El modelo nunca se carga en estos tests: se mockea
# `perplexity_detector._batch_sequence_perplexities`, que es el único punto
# por donde el modo segments llega a torch.

SEGMENT_HUMAN_SENTENCE = "El estudio revisa la práctica docente en escuelas rurales del país."
SEGMENT_AI_SENTENCE = "La propuesta robótica fortalece la gestión pedagógica de manera integral."


_SEGMENT_UNIQUE_COUNTER = itertools.count()


def _segment_unique_word() -> str:
    # Palabra distinta en cada llamada, solo letras (los dígitos cuentan
    # como ruido técnico).
    number = next(_SEGMENT_UNIQUE_COUNTER)
    letters = ""
    while True:
        letters = chr(ord("a") + number % 26) + letters
        number //= 26
        if not number:
            return f"vari{letters}"


def _segment_paragraph(sentence: str, words: int) -> str:
    """
    Repite `sentence` hasta ~`words` palabras, intercalando dos palabras
    únicas por oración para que ningún 8-grama se repita (si no, el
    segmento quedaría excluido como "repetido").
    """
    base = sentence.split()
    sentences = []

    for _ in range(max(1, words // (len(base) + 2))):
        varied = list(base)
        varied.insert(2, _segment_unique_word())
        varied.insert(8, _segment_unique_word())
        sentences.append(" ".join(varied))

    return " ".join(sentences)


def _fake_perplexities(texts: list[str]) -> list[float]:
    # "robótica" marca el texto de IA (perplejidad baja); el resto es humano.
    return [30.0 if "robótica" in text else 150.0 for text in texts]


class AISegmentsBuildTests(SimpleTestCase):
    def test_segments_respect_paragraphs_and_cover_whole_document(self) -> None:
        paragraphs = [_segment_paragraph(SEGMENT_HUMAN_SENTENCE, 110) for _ in range(9)]
        content = "\n\n".join(paragraphs)

        segments = ai_segments.build_segments(content)

        self.assertGreater(len(segments), 1)
        self.assertEqual(
            sum(segment.word_count for segment in segments),
            len(content.split()),
        )
        for segment in segments:
            # Ningún segmento empieza ni termina a mitad de párrafo.
            self.assertTrue(segment.start == 0 or content[segment.start - 2:segment.start] == "\n\n")
            self.assertTrue(segment.end == len(content) or content[segment.end:segment.end + 2] == "\n\n")
            self.assertLessEqual(segment.word_count, ai_segments.SEGMENT_MAX_WORDS)

    def test_long_paragraph_without_breaks_is_split_by_sentences(self) -> None:
        content = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 900)

        segments = ai_segments.build_segments(content)

        self.assertGreaterEqual(len(segments), 4)
        for segment in segments:
            self.assertTrue(content[segment.start:segment.end].endswith("país."))

    def test_does_not_split_sentence_after_et_al(self) -> None:
        sentences = ai_segments.split_sentences(
            "Según García Flores et al. (2022) el juego mejora la oralidad. "
            "Otros autores, p. ej. Ruiz, coinciden."
        )

        self.assertEqual(len(sentences), 2)
        self.assertTrue(sentences[0].startswith("Según García Flores et al. (2022)"))

    def test_figure_titles_and_quotes_are_not_evaluated(self) -> None:
        body = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 160)
        quote = "“esta cita textual no debe evaluarse jamás”"
        content = f"Tabla 2\nConvergencias identificadas en los estudios\n\n{body} {quote}"
        quote_start = content.index(quote)

        segments = ai_segments.build_segments(
            content,
            excluded_ranges=[(quote_start, quote_start + len(quote))],
        )

        self.assertEqual(len(segments), 1)
        text = segments[0].evaluable_text
        self.assertNotIn("Convergencias", text)
        self.assertNotIn("cita textual", text)
        self.assertEqual(segments[0].evaluable_word_count, len(body.split()))

    def test_english_or_technical_segment_is_not_evaluable(self) -> None:
        english = _segment_paragraph(
            "The results of this study show that games are used in the classroom.",
            170,
        )
        boolean = _segment_paragraph('("traditional games" OR "juegos") AND PRISMA 2020 SciELO.', 170)
        spanish = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 170)

        reasons = [
            segment.excluded_reason
            for segment in ai_segments.build_segments("\n\n".join([english, boolean, spanish]))
        ]

        self.assertEqual(
            reasons,
            [ai_segments.REASON_NOISE, ai_segments.REASON_NOISE, ""],
        )


class AISegmentsAggregateTests(SimpleTestCase):
    def _segment(self, words: int, perplexity: float | None, reason: str = ""):
        return ai_segments.TextSegment(
            start=0,
            end=0,
            word_count=words,
            evaluable_text="",
            evaluable_word_count=words,
            noise_ratio=0.0,
            sentences=[],
            perplexity=perplexity,
            excluded_reason=reason,
        )

    def test_percent_is_share_of_evaluable_words_in_ai_segments(self) -> None:
        segments = [
            self._segment(200, 30.0),  # score ~84 -> IA
            self._segment(100, 150.0),  # score ~0 -> humano
            self._segment(300, None, ai_segments.REASON_NOISE),  # no cuenta
        ]

        self.assertEqual(ai_segments.aggregate_ai_percent(segments), Decimal("66.67"))

    def test_midpoint_changes_result_without_rerunning_model(self) -> None:
        segments = [self._segment(200, 80.0), self._segment(200, 150.0)]

        self.assertEqual(ai_segments.aggregate_ai_percent(segments), Decimal("0.00"))
        self.assertEqual(
            ai_segments.aggregate_ai_percent(segments, midpoint=90.0),
            Decimal("50.00"),
        )

    def test_no_evaluable_words_gives_zero(self) -> None:
        segments = [self._segment(200, None, ai_segments.REASON_NOISE)]

        self.assertEqual(ai_segments.aggregate_ai_percent(segments), Decimal("0.00"))


class SegmentedPerplexityDetectorTests(SimpleTestCase):
    def test_whole_document_is_evaluated_and_breakdown_has_segments(self) -> None:
        # ~2000 palabras: muy por encima de los 900 tokens del modo global.
        paragraphs = [
            _segment_paragraph(
                SEGMENT_AI_SENTENCE if index >= 8 else SEGMENT_HUMAN_SENTENCE,
                100,
            )
            for index in range(20)
        ]
        content = "\n\n".join(paragraphs)

        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=_fake_perplexities,
        ):
            result = ai_segments.SegmentedPerplexityDetector().analyze(content)

        self.assertTrue(all(segment.perplexity is not None for segment in result.segments))
        ai_words = sum(len(p.split()) for p in paragraphs if "robótica" in p)
        expected = Decimal(ai_words * 100) / Decimal(len(content.split()))
        self.assertEqual(result.ai_probability_percent, expected.quantize(Decimal("0.01")))

        breakdown = result.to_breakdown()
        self.assertEqual(breakdown["segment_count"], len(result.segments))
        first = breakdown["segments"][0]
        self.assertEqual(
            set(first),
            {
                "start", "end", "words", "evaluable_words", "perplexity",
                "sentence_median_perplexity", "burstiness", "noise_ratio",
                "repeated_ratio", "score", "evaluable", "excluded_reason",
            },
        )
        self.assertEqual(first["sentence_median_perplexity"], 150.0)
        self.assertEqual(first["burstiness"], 0.0)
        self.assertTrue(first["evaluable"])

    def test_segment_without_perplexity_is_not_evaluable(self) -> None:
        content = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 200)

        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=lambda texts: [None] * len(texts),
        ):
            result = ai_segments.SegmentedPerplexityDetector().analyze(content)

        self.assertEqual(result.segments[0].excluded_reason, ai_segments.REASON_NO_PERPLEXITY)
        self.assertEqual(result.ai_probability_percent, Decimal("0.00"))


class AISegmentsProseOnlyTests(SimpleTestCase):
    """Solo cuenta la prosa corrida: repetición, listas, tablas y planes no."""

    def _reasons(self, *blocks: str) -> list[str]:
        return [
            segment.excluded_reason
            for segment in ai_segments.build_segments("\n\n".join(blocks))
        ]

    def test_repeated_segment_is_not_evaluable_but_first_occurrence_is(self) -> None:
        original = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 200)
        other = _segment_paragraph(SEGMENT_AI_SENTENCE, 200)

        segments = ai_segments.build_segments("\n\n".join([original, other, original]))

        self.assertEqual(
            [segment.excluded_reason for segment in segments],
            ["", "", ai_segments.REASON_REPEATED],
        )
        self.assertEqual(segments[2].repeated_ratio, 1.0)
        self.assertEqual(segments[0].repeated_ratio, 0.0)

    def test_near_duplicate_objectives_are_repeated_or_list(self) -> None:
        dimensions = ["la oralidad", "la escritura", "la comprensión lectora", "el cálculo"]
        objectives = "\n\n".join(
            "Determinar la influencia del aprendizaje basado en problemas en "
            f"{dimension} de los estudiantes de cuarto grado de la institución "
            "educativa del distrito de San Pablo durante el periodo escolar."
            for dimension in dimensions * 2
        )

        reasons = self._reasons(objectives)

        self.assertTrue(reasons)
        self.assertTrue(
            all(
                reason in {ai_segments.REASON_REPEATED, ai_segments.REASON_LIST}
                for reason in reasons
            ),
            reasons,
        )

    def test_hypotheses_and_uppercase_competencies_are_list(self) -> None:
        block = "\n".join(
            [
                "Ho: El juego no mejora la expresión oral de los niños de cinco años.",
                "H1: El juego mejora la expresión oral de los niños de cinco años.",
                "SE COMUNICA ORALMENTE EN SU LENGUA MATERNA y adecua sus ideas.",
                "LEE DIVERSOS TIPOS DE TEXTOS ESCRITOS en su lengua materna.",
                "ESCRIBE DIVERSOS TIPOS DE TEXTOS en su lengua materna con claridad.",
                "• Obtiene información del texto oral que escucha con atención.",
                "• Infiere e interpreta información del texto oral en clase.",
                "• Reflexiona sobre la forma y el contexto del texto oral.",
                "Capacidades del área",
                "Desempeños precisados",
            ]
        )

        self.assertEqual(self._reasons(block), [ai_segments.REASON_LIST])

    def test_session_plan_is_not_evaluable(self) -> None:
        plan = "\n".join(
            [
                "INICIO",
                "(15 min) La docente saluda a los estudiantes y presenta el propósito "
                "de la sesión mediante una pregunta retadora sobre el cuento leído.",
                "DESARROLLO",
                "(60 min) Los estudiantes forman equipos, leen el texto en voz alta y "
                "comentan las emociones que transmite cada personaje del relato.",
                "CIERRE",
                "(15 min) Se reflexiona con los estudiantes sobre lo aprendido y se "
                "registra el avance de cada equipo en la lista de cotejo del aula.",
            ]
        )

        self.assertEqual(self._reasons(plan), [ai_segments.REASON_SESSION_PLAN])

    def test_table_rows_are_not_evaluable(self) -> None:
        rows = "\n".join(
            ["Tabla 4", "Resultados del pretest y postest por dimensión"]
            + [
                f"Dimensión {name} del grupo experimental en el nivel logrado 12 45"
                for name in ("fluidez", "claridad", "coherencia", "volumen", "postura")
            ]
            + ["Nota. Elaboración propia con datos del instrumento aplicado en aula."]
        )

        self.assertEqual(self._reasons(rows), [ai_segments.REASON_TABLE])

    def test_long_labeled_prose_paragraph_is_still_prose(self) -> None:
        prose = "A. Dimensión física: " + _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 180)

        self.assertEqual(self._reasons(prose), [""])

    def test_excluded_words_do_not_count_and_reason_is_in_breakdown(self) -> None:
        prose = _segment_paragraph(SEGMENT_AI_SENTENCE, 200)
        content = "\n\n".join([prose, prose])

        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=_fake_perplexities,
        ):
            result = ai_segments.SegmentedPerplexityDetector().analyze(content)

        breakdown = result.to_breakdown()
        self.assertEqual(result.ai_probability_percent, Decimal("100.00"))
        self.assertEqual(breakdown["evaluable_words"], len(prose.split()))
        self.assertEqual(breakdown["total_words"], len(content.split()))
        self.assertEqual(
            breakdown["excluded_words_by_reason"],
            {ai_segments.REASON_REPEATED: len(prose.split())},
        )
        self.assertEqual(
            breakdown["segments"][1]["excluded_reason"],
            ai_segments.REASON_REPEATED,
        )


@skipUnless(importlib.util.find_spec("torch"), "torch no está instalado")
class PerplexityBatchMemoryBudgetTests(SimpleTestCase):
    """
    Presupuesto de memoria de `_batch_sequence_perplexities` con un GPT-2
    diminuto (sin descargar el modelo real) para las identidades, y la
    config real de DeepESP/gpt2-spanish para las cuentas del presupuesto.
    """

    REAL_CONFIG = SimpleNamespace(
        config=SimpleNamespace(n_embd=768, n_head=12, n_layer=12, vocab_size=50257)
    )

    def _tiny_model(self):
        import torch
        from transformers import GPT2Config, GPT2LMHeadModel

        torch.manual_seed(0)
        model = GPT2LMHeadModel(
            GPT2Config(n_layer=2, n_head=2, n_embd=16, vocab_size=97, n_positions=64)
        )
        return model.eval()

    def test_chunked_losses_match_full_logits(self) -> None:
        import torch
        from torch.nn import functional as F

        model = self._tiny_model()
        input_ids = torch.randint(0, 97, (3, 40))
        attention_mask = torch.ones_like(input_ids)
        attention_mask[1, 25:] = 0
        attention_mask[2, 9:] = 0

        with mock.patch.object(perplexity_module, "LOSS_CHUNK_TOKENS", 7), torch.no_grad():
            losses, counts = perplexity_module._mean_token_losses(
                model, input_ids, attention_mask
            )
            logits = model(input_ids, attention_mask=attention_mask).logits

        for row in range(3):
            real = int(attention_mask[row].sum())
            expected = F.cross_entropy(logits[row, : real - 1], input_ids[row, 1:real])
            self.assertEqual(int(counts[row]), real - 1)
            self.assertAlmostEqual(float(losses[row]), float(expected), places=4)

    def test_lm_head_never_sees_more_than_one_chunk(self) -> None:
        import torch

        model = self._tiny_model()
        seen_rows = []
        original_head = model.lm_head.forward

        def spy(hidden):
            seen_rows.append(hidden.shape[0])
            return original_head(hidden)

        model.lm_head.forward = spy
        input_ids = torch.randint(0, 97, (4, 50))

        with mock.patch.object(perplexity_module, "LOSS_CHUNK_TOKENS", 32), torch.no_grad():
            perplexity_module._mean_token_losses(model, input_ids, torch.ones_like(input_ids))

        self.assertEqual(sum(seen_rows), 4 * 49)
        self.assertLessEqual(max(seen_rows), 32)

    def test_budget_is_capped_even_with_lots_of_free_ram(self) -> None:
        with mock.patch("psutil.virtual_memory") as virtual_memory, mock.patch.object(
            perplexity_module, "_cgroup_memory_limit_and_usage_bytes", return_value=None
        ):
            virtual_memory.return_value.available = 64 * 1024**3
            budget = perplexity_module._available_batch_memory_budget_bytes()

        self.assertEqual(budget, perplexity_module.BATCH_MEMORY_MAX_BYTES)

    def test_estimate_counts_one_layer_of_attention_and_a_fixed_loss_chunk(self) -> None:
        estimate = perplexity_module._estimate_forward_pass_bytes

        # Los logits ya no crecen con el lote (trozo fijo de pérdida):
        # duplicar el lote no duplica la estimación.
        self.assertLess(
            estimate(2, 300, self.REAL_CONFIG),
            1.25 * estimate(1, 300, self.REAL_CONFIG),
        )
        # Picos medidos (MB sobre el modelo cargado, máximo de varias
        # corridas): la estimación debe quedar por encima.
        for batch, length, measured_mb in ((1, 900, 292), (4, 600, 684), (16, 300, 746), (32, 100, 604), (128, 30, 571)):
            self.assertGreater(estimate(batch, length, self.REAL_CONFIG), measured_mb * 1024**2)

    def test_thirty_thousand_word_thesis_fits_in_budget(self) -> None:
        # Tesis de ~30k palabras en modo segments: ~150 segmentos de ~200
        # palabras (~330 tokens) y ~1500 oraciones de ~30 tokens.
        lengths = [330] * 150 + [30] * 1500
        order = sorted(range(len(lengths)), key=lambda i: lengths[i])
        cap = perplexity_module.BATCH_MEMORY_MAX_BYTES

        with mock.patch.object(
            perplexity_module, "_available_batch_memory_budget_bytes", return_value=cap
        ):
            batches = list(
                perplexity_module._iter_batches_by_memory_budget(order, lengths, self.REAL_CONFIG)
            )

        self.assertEqual(sorted(i for batch in batches for i in batch), list(range(len(lengths))))
        for batch in batches:
            peak = perplexity_module._estimate_forward_pass_bytes(
                len(batch), max(lengths[i] for i in batch), self.REAL_CONFIG
            )
            self.assertTrue(len(batch) == 1 or peak <= cap)
        # El modelo cargado ronda 620-940MB de RSS: lote + modelo <= ~1.5GB.
        self.assertLessEqual(cap, 600 * 1024**2)

    def test_budget_is_read_again_before_each_batch(self) -> None:
        # Primer lote con 1 byte de presupuesto (lote mínimo de 1), el
        # segundo con RAM de sobra: el resto entra junto. El presupuesto se
        # lee al pedir cada lote, no todo al principio.
        lengths = [100] * 6
        budgets = iter([1, 10**12])
        calls = []

        def budget():
            calls.append(None)
            return next(budgets)

        with mock.patch.object(
            perplexity_module, "_available_batch_memory_budget_bytes", side_effect=budget
        ):
            batches = perplexity_module._iter_batches_by_memory_budget(
                list(range(6)), lengths, self.REAL_CONFIG
            )
            first = next(batches)
            self.assertEqual(len(calls), 1)
            second = next(batches)
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(batches), [])

        self.assertEqual(first, [0])
        self.assertEqual(second, [1, 2, 3, 4, 5])


class AIDetectionModeServiceTests(TestCase):
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
        self.document = make_test_document(self.institution, self.student)
        self.service = DocumentAnalysisService(requested_by=self.student)
        self.heuristic = mock.Mock(ai_probability_percent=Decimal("12.00"))
        self.content = "\n\n".join(
            _segment_paragraph(sentence, 200)
            for sentence in (SEGMENT_HUMAN_SENTENCE, SEGMENT_AI_SENTENCE)
        )

    def test_default_mode_is_global(self) -> None:
        self.assertEqual(settings.AI_DETECTION_MODE, "global")

    @override_settings(PERPLEXITY_AI_DETECTION_ENABLED=True, AI_DETECTION_MODE="global")
    def test_global_mode_keeps_previous_behavior(self) -> None:
        with mock.patch.object(
            self.service.perplexity_detector,
            "analyze",
            return_value=PerplexityAnalysisResult(
                ai_probability_percent=Decimal("3.83"),
                mean_perplexity=103.3,
                burstiness=None,
                sentence_count=10,
            ),
        ), mock.patch.object(
            self.service.segmented_perplexity_detector, "analyze"
        ) as segmented:
            percent, breakdown, detector = self.service._estimate_ai_probability(
                document=self.document,
                analysis_content=self.content,
                heuristic_result=self.heuristic,
            )

        segmented.assert_not_called()
        self.assertEqual(percent, Decimal("3.83"))
        self.assertEqual(detector, "perplexity-burstiness-v1")
        self.assertNotIn("segments", breakdown)

    @override_settings(PERPLEXITY_AI_DETECTION_ENABLED=True, AI_DETECTION_MODE="segments")
    def test_segments_mode_stores_segments_and_skips_quotes(self) -> None:
        quoted = (0, self.content.index(".") + 1)

        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=_fake_perplexities,
        ), mock.patch.object(self.service.perplexity_detector, "analyze") as global_analyze:
            percent, breakdown, detector = self.service._estimate_ai_probability(
                document=self.document,
                analysis_content=self.content,
                heuristic_result=self.heuristic,
                quoted_ranges=[quoted],
            )

        global_analyze.assert_not_called()
        self.assertEqual(detector, "perplexity-segments-v1")
        self.assertEqual(breakdown["mode"], "segments")
        self.assertEqual(breakdown["combination"], "perplexity-segments-replaces-heuristic")
        self.assertEqual(breakdown["segment_count"], 2)
        self.assertEqual(breakdown["segments"][0]["start"], 0)
        # La primera oración (citada) no cuenta como palabra evaluable.
        self.assertEqual(
            breakdown["segments"][0]["evaluable_words"],
            breakdown["segments"][0]["words"] - len(self.content[: quoted[1]].split()),
        )
        self.assertEqual(breakdown["perplexity_ai_score"], str(percent))
        self.assertGreater(percent, Decimal("40.00"))
        self.assertLess(percent, Decimal("60.00"))

    @override_settings(PERPLEXITY_AI_DETECTION_ENABLED=True, AI_DETECTION_MODE="segments")
    def test_segments_mode_falls_back_to_heuristic_on_error(self) -> None:
        with mock.patch.object(
            self.service.segmented_perplexity_detector,
            "analyze",
            side_effect=RuntimeError("sin torch"),
        ):
            percent, breakdown, detector = self.service._estimate_ai_probability(
                document=self.document,
                analysis_content=self.content,
                heuristic_result=self.heuristic,
            )

        self.assertEqual(percent, Decimal("12.00"))
        self.assertEqual(detector, "spanish-heuristic-v1")
        self.assertEqual(breakdown["combination"], "heuristic-only")


class EvaluateAIDetectionCommandTests(TestCase):
    def test_command_prints_table_and_midpoint_sweep(self) -> None:
        institution = Institution.objects.create(name="Instituto", slug="instituto")
        student = User.objects.create_user(
            username="alumno2",
            email="alumno2@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=institution,
        )
        document = make_test_document(institution, student, title="Proyecto Mantilla")
        AnalysisReport.objects.create(
            document=document,
            analyzed_content=_segment_paragraph(SEGMENT_AI_SENTENCE, 400),
        )
        stdout = StringIO()

        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=_fake_perplexities,
        ), mock.patch(
            "apps.analysis.management.commands.evaluate_ai_detection."
            "SpanishPerplexityDetector.analyze",
            return_value=PerplexityAnalysisResult(
                ai_probability_percent=Decimal("3.83"),
                mean_perplexity=103.3,
                burstiness=None,
                sentence_count=10,
            ),
        ):
            call_command(
                "evaluate_ai_detection",
                "--skip-bank",
                "--midpoints=55,90",
                f"--csv-path={settings.BASE_DIR / 'tmp_eval_test.csv'}",
                stdout=stdout,
                stderr=StringIO(),
            )

        (settings.BASE_DIR / "tmp_eval_test.csv").unlink()
        output = stdout.getvalue()
        self.assertIn("Proyecto Mantilla", output)
        self.assertIn("ia_01.txt", output)
        self.assertIn("humano_tesis_01.txt", output)
        self.assertIn("Barrido de MIDPOINT", output)
        self.assertIn("precisión=", output)


def _fake_global_analysis(content: str) -> PerplexityAnalysisResult:
    mean_perplexity = 30.0 if "robótica" in content else 150.0
    return PerplexityAnalysisResult(
        ai_probability_percent=perplexity_module._logistic_score(
            mean_perplexity,
            midpoint=perplexity_module.PERPLEXITY_MIDPOINT,
            scale=perplexity_module.PERPLEXITY_SCALE,
        ),
        mean_perplexity=mean_perplexity,
        burstiness=None,
        sentence_count=10,
    )


class EvaluateAIDetectionDirTests(SimpleTestCase):
    """--dir: banco privado de tesis en humanos/ e ia/ (eval_ia/)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from docx import Document as DocxDocument

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "humanos").mkdir()
        (self.root / "ia").mkdir()

        human = _segment_paragraph(SEGMENT_HUMAN_SENTENCE, 200)
        ai = _segment_paragraph(SEGMENT_AI_SENTENCE, 200)

        # Humano "sospechoso": 1 de 3 segmentos parece IA (~33 % >= 20).
        docx = DocxDocument()
        for paragraph in (human, ai, human):
            docx.add_paragraph(paragraph)
        docx.save(self.root / "humanos" / "tesis_mixta.docx")

        # La bibliografía no debe llegar al detector.
        (self.root / "humanos" / "tesis_limpia.txt").write_text(
            f"{human}\n\n{human}\n\nREFERENCIAS BIBLIOGRÁFICAS\n\n"
            "Pérez, J. (2020). La robótica educativa en el aula. Lima: Editorial Andina.",
            encoding="utf-8",
        )
        (self.root / "humanos" / "notas.md").write_text("se ignora", encoding="utf-8")
        (self.root / "ia" / "generado.txt").write_text(f"{ai}\n\n{ai}", encoding="utf-8")

    def _run(self, *args: str) -> str:
        stdout = StringIO()
        with mock.patch.object(
            perplexity_module,
            "_batch_sequence_perplexities",
            side_effect=_fake_perplexities,
        ), mock.patch(
            "apps.analysis.management.commands.evaluate_ai_detection."
            "SpanishPerplexityDetector.analyze",
            side_effect=_fake_global_analysis,
        ):
            call_command(
                "evaluate_ai_detection",
                f"--dir={self.root}",
                *args,
                stdout=stdout,
                stderr=StringIO(),
            )
        return stdout.getvalue()

    def test_summary_per_midpoint_with_fp_recall_and_medians(self) -> None:
        output = self._run()

        rows = {
            tuple(line.split()[:2]): line.split()[2:]
            for line in output.splitlines()
            if re.match(r"\s*\d+ (global|segments) ", line)
        }
        self.assertEqual(
            sorted({midpoint for midpoint, _ in rows}, key=int),
            ["55", "70", "80", "90"],
        )
        # segments: el DOCX mixto es falso positivo; global solo ve el inicio.
        fp, recall, human_median, ai_median = rows[("55", "segments")]
        self.assertEqual(fp, "1/2=0.50")
        self.assertEqual(recall, "1/1=1.00")
        self.assertEqual(ai_median, "100.00")
        self.assertEqual(rows[("55", "global")][0], "1/2=0.50")

    def test_csv_detail_per_file(self) -> None:
        import csv

        self._run("--midpoints=55,90")

        with (self.root / "resultados_ia.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(
            [(row["archivo"], row["esperado"]) for row in rows],
            [
                ("tesis_limpia.txt", "humano"),
                ("tesis_mixta.docx", "humano"),
                ("generado.txt", "IA"),
            ],
        )
        self.assertIn("pct_global_90", rows[0])
        self.assertIn("pct_segments_90", rows[0])
        self.assertEqual(rows[0]["pct_segments_55"], "0.00")
        self.assertEqual(rows[2]["pct_segments_55"], "100.00")

    def test_top_humans_list_their_most_ai_segments(self) -> None:
        output = self._run("--top-humans=1")

        top = output.split("Humanos con mayor % IA")[1]
        self.assertIn("tesis_mixta.docx", top)
        self.assertNotIn("tesis_limpia.txt", top)
        self.assertEqual(top.count("ppl="), 2)
        self.assertIn("ppl=30.0", top)
        self.assertIn("…", top)

    def test_samples_use_analysis_filter(self) -> None:
        from apps.analysis.management.commands.evaluate_ai_detection import Command

        samples = Command(stderr=StringIO())._file_samples(self.root / "humanos", "humano")

        self.assertEqual([s.name for s in samples], ["tesis_limpia.txt", "tesis_mixta.docx"])
        self.assertNotIn("Pérez, J. (2020)", samples[0].content)
        self.assertIn("\n\n", samples[1].content)

    def test_dir_without_subfolders_fails(self) -> None:
        with self.assertRaises(CommandError):
            call_command("evaluate_ai_detection", f"--dir={self.root / 'humanos'}")


def _paragraph(*sentences: str) -> str:
    return " ".join(sentences)


@override_settings(WEB_ANALYSIS_MAX_QUERY_PHRASES=1)
class WebSearchPhraseSelectionTests(SimpleTestCase):
    """
    Tesis Mantilla: buscar la oración más larga (30+ palabras) entre
    comillas daba 0 % web. Ahora se buscan ventanas de 8-14 palabras sin
    citas, números, siglas ni palabras vacías en los bordes.
    """

    CONTENT_SENTENCE = (
        "El juego tradicional favorece la expresión oral espontánea mediante "
        "interacciones lúdicas normadas culturalmente en comunidades rurales andinas."
    )
    OTHER_SENTENCE = (
        "Los docentes valoraron positivamente las rondas infantiles como "
        "estrategia didáctica pertinente para fortalecer vocabulario y fluidez."
    )

    def _groups(self, content: str) -> list[list[str]]:
        return WebCandidateCollector()._extract_search_phrases(content=content)

    def _all_phrases(self, content: str) -> list[str]:
        return [phrase for group in self._groups(content) for phrase in group]

    def test_phrases_are_short_windows_inside_one_sentence(self) -> None:
        content = _paragraph(self.CONTENT_SENTENCE, self.OTHER_SENTENCE)

        for phrase in self._all_phrases(content):
            self.assertTrue(8 <= len(phrase.split()) <= 14, phrase)
            self.assertIn(phrase, content)
            self.assertNotIn(". ", phrase)

    def test_second_phrase_comes_from_a_different_sentence(self) -> None:
        content = _paragraph(self.CONTENT_SENTENCE, self.OTHER_SENTENCE)

        [group] = self._groups(content)

        self.assertEqual(len(group), 2)
        self.assertIn(group[0], self.CONTENT_SENTENCE + self.OTHER_SENTENCE)
        self.assertNotEqual(
            group[0] in self.CONTENT_SENTENCE,
            group[1] in self.CONTENT_SENTENCE,
        )

    def test_windows_never_include_citations_numbers_or_acronyms(self) -> None:
        content = _paragraph(
            "Según Quiroz-Sagardia (2025) y Rojas et al. 2024 los juegos "
            "tradicionales operaron como herramientas MINEDU de socialización "
            "temprana en 45 instituciones educativas públicas del país.",
            self.CONTENT_SENTENCE,
        )

        for phrase in self._all_phrases(content):
            self.assertNotRegex(phrase, r"\d|\(|\)|et al|MINEDU")

    def test_windows_do_not_start_or_end_with_stopwords(self) -> None:
        content = _paragraph(self.CONTENT_SENTENCE, self.OTHER_SENTENCE)

        for phrase in self._all_phrases(content):
            words = [word.strip(".,;:").lower() for word in phrase.split()]
            self.assertNotIn(words[0], {"el", "la", "los", "de", "en", "y", "para"})
            self.assertNotIn(words[-1], {"el", "la", "los", "de", "en", "y", "para"})

    def test_english_abstract_is_not_searched(self) -> None:
        abstract = (
            "It was concluded that traditional games constitute highly valuable "
            "pedagogical and sociocultural mediators that foster oral communicative "
            "competence in elementary education, provided their implementation follows "
            "an intentional and contextualized teaching plan."
        )

        self.assertEqual(self._groups(abstract), [])

    def test_prefers_content_words_over_stopword_heavy_windows(self) -> None:
        content = _paragraph(
            "Esto es lo que se hizo en el aula de la escuela para que los niños "
            "lo vean y lo usen en su casa con sus padres y con sus amigos.",
            self.CONTENT_SENTENCE,
        )

        [group] = self._groups(content)

        self.assertIn(group[0], self.CONTENT_SENTENCE)


class _ProbeFakeFetcher:
    def __init__(self, texts_by_url: dict[str, str]) -> None:
        self.texts_by_url = texts_by_url

    def fetch(self, result: WebSearchResult) -> WebPageContent | None:
        return WebPageContent(
            title=result.title,
            url=result.url,
            domain=result.domain,
            text=self.texts_by_url.get(result.url, "texto sin relacion"),
            source_format="HTML",
        )


@override_settings(
    BRAVE_SEARCH_API_KEY="clave-de-prueba",
    WEB_ANALYSIS_MAX_QUERY_PHRASES=8,
    WEB_ANALYSIS_MAX_QUERIES=24,
    WEB_ANALYSIS_MAX_PAGES_TOTAL=16,
    WEB_ANALYSIS_MAX_PAGES_PER_QUERY=2,
)
class WebSearchProbeCommandTests(TestCase):
    def setUp(self) -> None:
        institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-de-prueba",
        )
        student = User.objects.create_user(
            username="alumno-probe",
            email="alumno-probe@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=institution,
        )
        self.content = "\n\n".join(
            _body_paragraph(index) for index in range(16)
        )
        self.document = Document(
            institution=institution,
            owner=student,
            uploaded_by=student,
            title="Tesis probe",
            kind=DocumentKind.OTHER,
            original_filename="tesis.pdf",
            mime_type="application/pdf",
            file_size_bytes=10,
            sha256_hash="hash-probe",
        )
        self.document.original_file.save(
            "tesis.pdf",
            ContentFile(b"%PDF-1.4"),
            save=False,
        )
        self.document.save()
        AnalysisReport.objects.create(
            document=self.document,
            analyzed_content=self.content,
        )

    def _call(self, *args: str) -> str:
        stdout = StringIO()
        call_command("web_search_probe", str(self.document.id), *args, stdout=stdout)
        return stdout.getvalue()

    def test_dry_run_prints_queries_of_both_strategies_without_searching(self) -> None:
        with mock.patch(
            "apps.analysis.engines.web_search.BraveWebSearchClient.search",
            side_effect=AssertionError("dry-run no debe llamar a Brave"),
        ):
            output = self._call("--dry-run", "--estrategia", "ambas")

        self.assertIn("=== Estrategia anterior ===", output)
        self.assertIn("=== Estrategia nueva ===", output)
        self.assertIn("site:repositorio.ucv.edu.pe", output)
        new_section = output.split("=== Estrategia nueva ===")[1]
        self.assertNotIn("site:", new_section)
        self.assertNotIn("filetype:", new_section)
        self.assertRegex(output, r"Consultas generadas\s+24\s+24")

    def test_counts_shared_eight_word_sequences_per_page(self) -> None:
        words = self.content.split()
        # Página 1: comparte 3 secuencias de 8 palabras (10 palabras seguidas).
        # Página 2: comparte 1 secuencia. El resto, ninguna.
        texts_by_url = {
            "https://example.com/1/0": "relleno " + " ".join(words[100:110]) + " relleno",
            "https://example.com/1/1": " ".join(words[300:308]),
        }

        def fake_collector(command_self) -> WebCandidateCollector:
            collector = WebCandidateCollector()
            collector.search_client = _FakeSearchClient()
            collector.fetcher = _ProbeFakeFetcher(texts_by_url)
            return collector

        with mock.patch(
            "apps.analysis.management.commands.web_search_probe.Command._collector",
            fake_collector,
        ):
            output = self._call("--estrategia", "ambas")

        self.assertIn("[   3 secuencias] https://example.com/1/0", output)
        self.assertIn("[   1 secuencias] https://example.com/1/1", output)
        self.assertRegex(output, r"Páginas con ≥1 secuencia de 8\s+2\s+2")
        self.assertRegex(output, r"Páginas con ≥3 secuencias de 8\s+1\s+1")
        self.assertIn("Páginas (anterior)", output)
        self.assertIn("Páginas (nueva)", output)

    def test_without_api_key_requires_dry_run(self) -> None:
        with override_settings(BRAVE_SEARCH_API_KEY=""):
            with self.assertRaises(CommandError):
                self._call()

    def test_unknown_document_raises_command_error(self) -> None:
        with self.assertRaises(CommandError):
            call_command("web_search_probe", str(uuid4()), "--dry-run", stdout=StringIO())


_UPEU_REPOSITORY = OaiRepositoryConfig(
    name="Repositorio UPeU",
    domain="repositorio.upeu.edu.pe",
    base_url="https://repositorio.upeu.edu.pe/server/oai/request",
)
_UPEU_SETTINGS = [
    {
        "name": _UPEU_REPOSITORY.name,
        "domain": _UPEU_REPOSITORY.domain,
        "base_url": _UPEU_REPOSITORY.base_url,
    }
]


def _oai_record(number: int = 1, title: str = "Tesis de educación") -> OaiRecord:
    return OaiRecord.objects.create(
        repository_name=_UPEU_REPOSITORY.name,
        repository_domain=_UPEU_REPOSITORY.domain,
        oai_identifier=f"oai:repositorio.upeu.edu.pe:20.500.12840/{number}",
        title=title,
        source_url=f"https://hdl.handle.net/20.500.12840/{number}",
        text_excerpt="Resumen breve de la tesis.",
        normalized_text="resumen breve de la tesis",
        harvested_at=timezone.now(),
    )


class FingerprintPositionTests(SimpleTestCase):
    def test_positions_have_the_same_fingerprints(self) -> None:
        text = _synthetic_text(1, 300)

        self.assertEqual(
            set(compute_fingerprint_positions(text)),
            compute_fingerprints(text),
        )

    def test_short_text_keeps_the_same_fingerprints(self) -> None:
        for text in ("uno dos", "uno dos tres cuatro cinco"):
            self.assertEqual(
                set(compute_fingerprint_positions(text)),
                compute_fingerprints(text),
            )

    def test_position_is_the_first_appearance(self) -> None:
        repeated = _synthetic_text(2, 40)
        text = f"{repeated} {_synthetic_text(3, 40)} {repeated}"
        limit = len(text.split()) - 40

        positions = compute_fingerprint_positions(text)

        for hash_value in compute_fingerprints(repeated):
            self.assertLess(positions[hash_value], limit)


class OaiFullTextIndexerTests(TestCase):
    def test_blocks_rebuild_the_text(self) -> None:
        text = _synthetic_text(4, 500)

        chunks, _ = OaiFullTextIndexer().index(_oai_record(), content=text)

        stored = OaiFullTextChunk.objects.order_by("position")
        self.assertEqual(chunks, 3)
        self.assertEqual("".join(chunk.text for chunk in stored), text)

    def test_each_fingerprint_is_stored_once_per_record(self) -> None:
        phrase = _synthetic_text(5, 60)
        text = f"{phrase} {_synthetic_text(6, 300)} {phrase}"
        expected = compute_fingerprints(text)

        OaiFullTextIndexer().index(_oai_record(), content=text)

        rows = DocumentFingerprint.objects.filter(
            source_type=SOURCE_TYPE_OAI_FULLTEXT,
        ).values_list("hash", flat=True)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual(set(rows), expected)

    def test_reindexing_replaces_previous_rows(self) -> None:
        record = _oai_record()
        indexer = OaiFullTextIndexer()
        indexer.index(record, content=_synthetic_text(7, 400))

        _, fingerprints = indexer.index(record, content=_synthetic_text(8, 200))

        self.assertEqual(OaiFullTextChunk.objects.count(), 2)
        self.assertEqual(
            DocumentFingerprint.objects.filter(
                source_type=SOURCE_TYPE_OAI_FULLTEXT,
            ).count(),
            fingerprints,
        )


class OaiFullTextRetrievalTests(TestCase):
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
        self.current = make_test_document(
            self.institution,
            self.student,
            title="Documento actual",
        )
        self.text = _synthetic_text(11, 2000)
        self.record = _oai_record()
        OaiFullTextIndexer().index(self.record, content=self.text)

    def _span_percent(self, content: str) -> Decimal:
        retrieved = CandidateRetriever().retrieve(self.current, content)
        return (
            SpanSimilarityEngine(min_match_words=8, max_gap_words=2)
            .analyze(content, retrieved.to_span_sources())
            .similarity_percent
        )

    def test_full_copy_matches_one_source_at_100_percent(self) -> None:
        retrieved = CandidateRetriever().retrieve(self.current, self.text)

        self.assertEqual(
            [source.source_id for source in retrieved.sources],
            [self.record.id],
        )
        self.assertEqual(self._span_percent(self.text), Decimal("100.00"))

    def test_only_blocks_with_hits_and_neighbours_are_loaded(self) -> None:
        words = self.text.split()
        content = " ".join(words[900:950])

        source = CandidateRetriever().retrieve(self.current, content).sources[0]

        self.assertLessEqual(len(source.oai_chunks), 4)
        self.assertLess(len(source.oai_chunks), OaiFullTextChunk.objects.count())

    def test_match_crossing_two_blocks_is_found(self) -> None:
        words = self.text.split()
        boundary = OaiFullTextIndexer.BLOCK_WORD_SIZE * 4
        content = " ".join(words[boundary - 10 : boundary + 10])

        self.assertEqual(self._span_percent(content), Decimal("100.00"))

    def test_document_frequency_counts_each_thesis_once(self) -> None:
        phrase = _synthetic_text(12, 50)
        record = _oai_record(2)
        OaiFullTextIndexer().index(
            record,
            content=f"{phrase} {_synthetic_text(13, 400)} {phrase}",
        )

        retrieved = CandidateRetriever(max_document_frequency=1).retrieve(
            self.current,
            phrase,
        )

        self.assertEqual(
            [source.source_id for source in retrieved.sources],
            [record.id],
        )

    def test_inactive_record_is_ignored(self) -> None:
        OaiRecord.objects.filter(id=self.record.id).update(is_active=False)

        self.assertEqual(
            CandidateRetriever().retrieve(self.current, self.text).sources,
            [],
        )

    @override_settings(OAI_HARVEST_ENABLED=False)
    def test_oai_disabled_ignores_full_text(self) -> None:
        self.assertEqual(
            CandidateRetriever().retrieve(self.current, self.text).sources,
            [],
        )

    def test_chunk_engine_receives_the_blocks(self) -> None:
        retrieved = CandidateRetriever().retrieve(self.current, self.text)

        candidates = retrieved.to_chunk_candidates(max_candidates=500)

        self.assertIn(
            OaiFullTextIndexer().text_indexer._normalize(
                OaiFullTextChunk.objects.get(
                    record=self.record,
                    position=2,
                ).text
            ),
            [candidate.normalized_text for candidate in candidates],
        )

    def test_old_retrieval_ignores_full_text_fingerprints(self) -> None:
        candidates, records = DocumentAnalysisService(
            requested_by=self.student,
        )._get_fingerprint_candidates(
            document=self.current,
            analysis_content=self.text,
        )

        self.assertEqual(candidates, [])
        self.assertEqual(records, {})


class _FakeHttpResponse:
    def __init__(self, status_code=200, body=b"", headers=None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.is_redirect = False
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, routes) -> None:
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.routes[url]


class PoliteClientTests(SimpleTestCase):
    HOST = "https://repo.test"

    def _client(self, routes, robots=b"User-agent: *\nDisallow: /privado\n"):
        self.now = 100.0
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds

        routes.setdefault(
            f"{self.HOST}/robots.txt",
            _FakeHttpResponse(body=robots),
        )
        session = _FakeSession(routes)
        return PoliteClient(session=session, sleep=sleep, clock=lambda: self.now), session

    def test_requests_are_spaced_and_identified(self) -> None:
        client, session = self._client(
            {
                f"{self.HOST}/a": _FakeHttpResponse(body=b"a"),
                f"{self.HOST}/b": _FakeHttpResponse(body=b"b"),
            }
        )

        client.get(f"{self.HOST}/a", max_bytes=10)
        client.get(f"{self.HOST}/b", max_bytes=10)

        self.assertEqual(self.sleeps, [3.0, 3.0])
        self.assertTrue(
            all(
                "IESPPAB" in kwargs["headers"]["User-Agent"]
                for _, kwargs in session.calls
            )
        )

    def test_robots_is_fetched_once_per_host(self) -> None:
        client, session = self._client(
            {
                f"{self.HOST}/a": _FakeHttpResponse(body=b"a"),
                f"{self.HOST}/b": _FakeHttpResponse(body=b"b"),
            }
        )

        client.get(f"{self.HOST}/a", max_bytes=10)
        client.get(f"{self.HOST}/b", max_bytes=10)

        urls = [url for url, _ in session.calls]
        self.assertEqual(urls.count(f"{self.HOST}/robots.txt"), 1)

    def test_disallowed_url_is_not_requested(self) -> None:
        client, session = self._client({})

        with self.assertRaises(FullTextFetchError):
            client.get(f"{self.HOST}/privado/x", max_bytes=10)

        self.assertNotIn(f"{self.HOST}/privado/x", [url for url, _ in session.calls])

    def test_redirect_to_disallowed_url_is_not_followed(self) -> None:
        client, session = self._client(
            {
                f"{self.HOST}/a": _FakeHttpResponse(
                    status_code=302,
                    headers={"Location": "/privado/x"},
                ),
            }
        )

        with self.assertRaises(FullTextFetchError):
            client.get(f"{self.HOST}/a", max_bytes=10)

        self.assertNotIn(f"{self.HOST}/privado/x", [url for url, _ in session.calls])

    def test_each_redirect_counts_as_a_spaced_request(self) -> None:
        client, _ = self._client(
            {
                f"{self.HOST}/a": _FakeHttpResponse(
                    status_code=301,
                    headers={"Location": "/b"},
                ),
                f"{self.HOST}/b": _FakeHttpResponse(body=b"ok"),
            }
        )

        response = client.get(f"{self.HOST}/a", max_bytes=10)

        self.assertEqual(response.url, f"{self.HOST}/b")
        self.assertEqual(self.sleeps, [3.0, 3.0])

    def test_declared_size_over_limit_is_not_read(self) -> None:
        response = _FakeHttpResponse(body=b"x", headers={"Content-Length": "999"})
        client, _ = self._client({f"{self.HOST}/a": response})

        with self.assertRaises(ResponseTooLarge):
            client.get(f"{self.HOST}/a", max_bytes=10)

        self.assertTrue(response.closed)

    def test_real_size_over_limit_stops_reading(self) -> None:
        client, _ = self._client(
            {f"{self.HOST}/a": _FakeHttpResponse(body=b"x" * 100)}
        )
        client.CHUNK_BYTES = 8

        with self.assertRaises(ResponseTooLarge):
            client.get(f"{self.HOST}/a", max_bytes=10)

    def test_throttling_stops_the_harvest(self) -> None:
        client, _ = self._client({f"{self.HOST}/a": _FakeHttpResponse(status_code=429)})

        with self.assertRaises(RepositoryThrottled):
            client.get(f"{self.HOST}/a", max_bytes=10)

    def test_unreadable_robots_blocks_everything(self) -> None:
        client, session = self._client(
            {f"{self.HOST}/robots.txt": _FakeHttpResponse(status_code=500)}
        )

        with self.assertRaises(RobotsUnavailable):
            client.get(f"{self.HOST}/a", max_bytes=10)

        self.assertEqual(len(session.calls), 1)

    def test_missing_robots_allows_access(self) -> None:
        client, _ = self._client(
            {
                f"{self.HOST}/robots.txt": _FakeHttpResponse(status_code=404),
                f"{self.HOST}/a": _FakeHttpResponse(body=b"ok"),
            }
        )

        self.assertEqual(client.get(f"{self.HOST}/a", max_bytes=10).content, b"ok")


class _FakeHarvestClient:
    PDF_URL = "https://repositorio.upeu.edu.pe/bitstreams/abc/download"

    def __init__(
        self,
        *,
        pdf_url=PDF_URL,
        rights=("http://creativecommons.org/licenses/by-nc-sa/4.0/",),
        dates=(),
        pdf_error=None,
    ) -> None:
        self.pdf_url = pdf_url
        self.rights = rights
        self.dates = dates
        self.pdf_error = pdf_error
        self.urls = []

    def get(self, url, *, max_bytes, params=None):
        self.urls.append(url)

        if url == self.PDF_URL:
            if self.pdf_error:
                raise self.pdf_error
            return FetchedResponse(200, url, b"%PDF-1.7 contenido")

        if params:
            items = "".join(
                f"<dc:rights>{value}</dc:rights>" for value in self.rights
            ) + "".join(f"<dc:date>{value}</dc:date>" for value in self.dates)
            xml = (
                '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<GetRecord><record><metadata>{items}</metadata></record>"
                "</GetRecord></OAI-PMH>"
            )
            return FetchedResponse(200, url, xml.encode())

        meta = (
            f'<meta name="citation_pdf_url" content="{self.pdf_url}">'
            if self.pdf_url
            else ""
        )
        html = (
            f'<html><head>{meta}<meta name="citation_publication_date" '
            'content="2021-07-03"></head><body></body></html>'
        )
        return FetchedResponse(200, url, html.encode())


class OaiFullTextHarvesterTests(TestCase):
    def setUp(self) -> None:
        self.record = _oai_record()

    def _harvest(self, client, pages=None, filtered_words=2000):
        pages = pages if pages is not None else ["uno dos tres cuatro " * 50] * 40
        harvester = OaiFullTextHarvester(_UPEU_REPOSITORY, client=client)
        filtered = SimpleNamespace(content=_synthetic_text(21, filtered_words))

        with (
            mock.patch(
                "apps.analysis.engines.oai_fulltext.read_pdf_pages",
                return_value=pages,
            ),
            mock.patch.object(
                harvester.text_filter,
                "filter_for_similarity",
                return_value=filtered,
            ),
        ):
            return harvester.harvest(self.record)

    def test_ok_stores_metadata_and_indexes(self) -> None:
        full_text = self._harvest(_FakeHarvestClient())

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(full_text.publication_year, 2021)
        self.assertEqual(
            full_text.rights,
            "http://creativecommons.org/licenses/by-nc-sa/4.0/",
        )
        self.assertEqual(full_text.pdf_url, _FakeHarvestClient.PDF_URL)
        self.assertEqual(full_text.word_count, 2000)
        self.assertEqual(self.record.full_text_chunks.count(), 12)

    def test_handle_is_resolved_in_the_repository(self) -> None:
        client = _FakeHarvestClient()

        self._harvest(client)

        self.assertEqual(
            client.urls[0],
            "https://repositorio.upeu.edu.pe/handle/20.500.12840/1",
        )

    def test_without_pdf_meta_is_sin_pdf(self) -> None:
        client = _FakeHarvestClient(pdf_url="")

        full_text = self._harvest(client)

        self.assertEqual(full_text.status, OaiFullTextStatus.SIN_PDF)
        self.assertEqual(full_text.publication_year, 2021)
        self.assertEqual(self.record.full_text_chunks.count(), 0)

    def test_image_pdf_is_escaneado(self) -> None:
        full_text = self._harvest(_FakeHarvestClient(), pages=["Portada"] + [""] * 80)

        self.assertEqual(full_text.status, OaiFullTextStatus.ESCANEADO)
        self.assertEqual(self.record.full_text_chunks.count(), 0)

    def test_short_pdf_is_not_indexed(self) -> None:
        full_text = self._harvest(_FakeHarvestClient(), filtered_words=400)

        self.assertEqual(full_text.status, OaiFullTextStatus.TEXTO_INSUFICIENTE)
        self.assertEqual(self.record.full_text_chunks.count(), 0)

    def test_active_embargo_does_not_download_the_pdf(self) -> None:
        next_year = timezone.now().year + 1
        client = _FakeHarvestClient(
            rights=("info:eu-repo/semantics/embargoedAccess",),
            dates=(f"info:eu-repo/date/embargoEnd/{next_year}-01-01",),
        )

        full_text = self._harvest(client)

        self.assertEqual(full_text.status, OaiFullTextStatus.EMBARGADO)
        self.assertEqual(full_text.embargo_end, date(next_year, 1, 1))
        self.assertNotIn(_FakeHarvestClient.PDF_URL, client.urls)

    def test_expired_embargo_is_harvested(self) -> None:
        client = _FakeHarvestClient(
            rights=("info:eu-repo/semantics/embargoedAccess",),
            dates=("info:eu-repo/date/embargoEnd/2020-01-01",),
        )

        self.assertEqual(self._harvest(client).status, OaiFullTextStatus.OK)

    def test_restricted_without_date_is_not_downloaded(self) -> None:
        client = _FakeHarvestClient(
            rights=("info:eu-repo/semantics/restrictedAccess",),
        )

        self.assertEqual(self._harvest(client).status, OaiFullTextStatus.EMBARGADO)
        self.assertNotIn(_FakeHarvestClient.PDF_URL, client.urls)

    def test_oversized_pdf_is_not_retried(self) -> None:
        client = _FakeHarvestClient(pdf_error=ResponseTooLarge("30 MB"))

        full_text = self._harvest(client)

        self.assertEqual(full_text.status, OaiFullTextStatus.SIN_PDF)
        self.assertIn("20 MB", full_text.error_message)

    def test_network_error_is_recorded(self) -> None:
        client = _FakeHarvestClient(pdf_error=FullTextFetchError("Error de red"))

        full_text = self._harvest(client)

        self.assertEqual(full_text.status, OaiFullTextStatus.ERROR)
        self.assertIn("Error de red", full_text.error_message)

    def test_throttling_propagates_without_saving(self) -> None:
        client = _FakeHarvestClient(pdf_error=RepositoryThrottled("429"))

        with self.assertRaises(RepositoryThrottled):
            self._harvest(client)

        self.assertFalse(OaiFullText.objects.exists())

    def test_retry_replaces_previous_state(self) -> None:
        self._harvest(_FakeHarvestClient(pdf_error=FullTextFetchError("Error de red")))

        full_text = self._harvest(_FakeHarvestClient())

        self.assertEqual(full_text.status, OaiFullTextStatus.OK)
        self.assertEqual(full_text.attempts, 2)
        self.assertEqual(full_text.error_message, "")


@override_settings(OAI_HARVEST_REPOSITORIES=_UPEU_SETTINGS)
class HarvestOaiFullTextCommandTests(TestCase):
    def setUp(self) -> None:
        self.records = [
            _oai_record(number, title=f"Tesis de educación {number}")
            for number in range(1, 5)
        ]
        _oai_record(9, title="Tesis de ingeniería")

    def _call(self, outcomes=None, **options) -> tuple[list[str], str]:
        """Devuelve los identificadores procesados y la salida del comando."""
        output = StringIO()
        seen: list[str] = []
        outcomes = iter(outcomes or [])

        def harvest(harvester, record):
            seen.append(record.oai_identifier)
            status, attempts_error = next(outcomes, (OaiFullTextStatus.SIN_PDF, None))

            if attempts_error is not None:
                raise attempts_error

            previous = OaiFullText.objects.filter(record=record).first()
            full_text, _ = OaiFullText.objects.update_or_create(
                record=record,
                defaults={
                    "status": status,
                    "attempts": previous.attempts + 1 if previous else 1,
                },
            )
            return full_text

        with mock.patch.object(OaiFullTextHarvester, "harvest", harvest):
            call_command(
                "harvest_oai_fulltext",
                repositorio="UPeU",
                filtro_titulo="educaci[oó]n",
                stdout=output,
                **options,
            )

        return seen, output.getvalue()

    def test_dry_run_does_not_touch_network_or_database(self) -> None:
        with mock.patch("requests.sessions.Session.send") as send:
            seen, output = self._call(limite=2, dry_run=True)

        send.assert_not_called()
        self.assertEqual(seen, [])
        self.assertIn("2 por procesar", output)
        self.assertFalse(OaiFullText.objects.exists())

    def test_title_filter_and_limit_are_applied(self) -> None:
        seen, _ = self._call(limite=2)

        self.assertEqual(len(seen), 2)
        self.assertNotIn("/9", "".join(seen))

    def test_rerun_continues_up_to_the_same_total(self) -> None:
        first, _ = self._call(limite=2)
        repeated, _ = self._call(limite=2)
        second, _ = self._call(limite=3)

        self.assertEqual(repeated, [])
        self.assertEqual(len(second), 1)
        self.assertNotIn(second[0], first)

    def test_errors_are_retried_up_to_three_times(self) -> None:
        error = (OaiFullTextStatus.ERROR, None)

        for _ in range(3):
            seen, _ = self._call([error], limite=1)
            self.assertEqual(len(seen), 1)

        seen, _ = self._call(limite=1)

        self.assertEqual(seen, [])

    def test_throttling_stops_and_keeps_progress(self) -> None:
        outcomes = [
            (OaiFullTextStatus.SIN_PDF, None),
            (None, RepositoryThrottled("429")),
        ]

        with self.assertRaises(CommandError):
            self._call(outcomes, limite=4)

        self.assertEqual(OaiFullText.objects.count(), 1)

    def test_consecutive_errors_stop_the_run(self) -> None:
        for number in range(10, 14):
            _oai_record(number, title=f"Tesis de educación {number}")

        with self.assertRaises(CommandError):
            self._call([(OaiFullTextStatus.ERROR, None)] * 8, limite=8)

        self.assertEqual(OaiFullText.objects.count(), 5)

    def test_title_exclusion_is_applied(self) -> None:
        _oai_record(20, title="Educación para la salud")

        seen, _ = self._call(limite=10, excluir_titulo="salud")

        self.assertEqual(len(seen), 4)
        self.assertNotIn(self.records[0].oai_identifier.replace("/1", "/20"), seen)

    def test_excluded_id_is_never_processed(self) -> None:
        excluded = self.records[0].oai_identifier

        seen, _ = self._call(limite=10, excluir_id=[excluded])

        self.assertEqual(len(seen), 3)
        self.assertNotIn(excluded, seen)

    def test_unknown_excluded_id_fails(self) -> None:
        with self.assertRaises(CommandError):
            self._call(limite=1, excluir_id=["oai:no-existe:1"])

    def test_already_indexed_excluded_id_fails(self) -> None:
        OaiFullTextIndexer().index(self.records[0], content=_synthetic_text(31, 200))

        with self.assertRaises(CommandError):
            self._call(limite=1, excluir_id=[self.records[0].oai_identifier])

    def test_expired_embargo_is_retried_and_future_one_is_not(self) -> None:
        today = timezone.localdate()

        for record, end in zip(
            self.records,
            (today, today + timedelta(days=1), None),
        ):
            OaiFullText.objects.create(
                record=record,
                status=OaiFullTextStatus.EMBARGADO,
                embargo_end=end,
            )

        seen, _ = self._call(limite=4)

        self.assertEqual(
            seen,
            [self.records[0].oai_identifier, self.records[3].oai_identifier],
        )

    def test_unknown_repository_fails(self) -> None:
        with self.assertRaises(CommandError):
            call_command("harvest_oai_fulltext", repositorio="UCV", limite=1)
