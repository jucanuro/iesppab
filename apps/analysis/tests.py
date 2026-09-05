from __future__ import annotations

from decimal import Decimal

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from apps.accounts.models import User, UserRole
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
from apps.analysis.models import DocumentFingerprint, DocumentKnowledgeChunk
from apps.analysis.services import DocumentAnalysisService
from apps.core.models import Institution
from apps.documents.models import Document, DocumentKind, DocumentText
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
