from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from uuid import uuid4

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, UserRole
from apps.analysis.engines.exclusions import calculate_similarity_percent
from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.similarity import (
    CandidateKnowledgeChunk,
    InternalSimilarityEngine,
    SimilarityMatch,
)
from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.engines.web_similarity import (
    WebSimilarityAnalysisResult,
    WebSimilarityMatch,
)
from apps.analysis.indexers import DocumentKnowledgeIndexer
from apps.analysis.models import (
    AnalysisJob,
    AnalysisJobStatus,
    DocumentFingerprint,
    DocumentKnowledgeChunk,
)
from apps.analysis.services import (
    MAX_ANALYSIS_DELIVERIES,
    DocumentAnalysisError,
    DocumentAnalysisService,
)
from apps.analysis.tasks import run_document_analysis
from apps.core.models import Institution
from apps.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentText,
)
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
        "Estrategias lúdicas para mejorar la expresión oral en estudiantes "
        "de educación inicial de una institución educativa de Trujillo",
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
