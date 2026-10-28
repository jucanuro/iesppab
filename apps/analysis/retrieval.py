from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from django.conf import settings
from django.db.models import Count

from apps.analysis.engines.exclusions import subtract_ranges
from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.similarity import (
    CandidateKnowledgeChunk,
    InternalSimilarityEngine,
)
from apps.analysis.engines.span_similarity import (
    SOURCE_KIND_INTERNAL,
    SOURCE_KIND_OAI,
    SpanSource,
)
from apps.analysis.models import (
    DocumentFingerprint,
    DocumentKnowledgeChunk,
    OaiRecord,
)
from apps.documents.models import Document

logger = logging.getLogger(__name__)

# Cantidad de parámetros por consulta `IN (...)`. SQLite admite 32766 desde
# 3.32 pero versiones viejas solo 999; con lotes de 900 ambas funcionan.
QUERY_BATCH_SIZE = 900


def compute_query_fingerprints(
    content: str,
    excluded_ranges: Sequence[tuple[int, int]],
    normalize: Callable[[str], str],
) -> set[int]:
    """
    Huellas del documento sin cruzar el borde de una cita excluida: cada
    tramo de prosa entre citas se normaliza y se huellea por separado.
    Los tramos con menos palabras que un shingle se omiten (sus "huellas"
    serían palabras sueltas, que solo agregan ruido).
    """
    segments = subtract_ranges([(0, len(content))], excluded_ranges)
    fingerprints: set[int] = set()

    for start, end in segments:
        normalized = normalize(content[start:end])

        if len(normalized.split()) < InternalSimilarityEngine.SHINGLE_SIZE:
            continue

        fingerprints.update(compute_fingerprints(normalized))

    return fingerprints


@dataclass
class RetrievedSource:
    kind: str
    source_id: UUID
    shared_rare_hashes: int
    title: str = ""
    owner_name: str = ""
    # Internos: fragmentos con huellas raras en común, ordenados por offset.
    chunks: list[DocumentKnowledgeChunk] = field(default_factory=list)
    oai_record: OaiRecord | None = None


@dataclass
class RetrievalResult:
    sources: list[RetrievedSource]
    oai_records_by_id: dict[UUID, OaiRecord]
    query_fingerprint_count: int = 0
    rare_fingerprint_count: int = 0
    # Hits (fragmento interno o registro OAI) por huella rara, para ordenar
    # los candidatos del motor chunk.
    rare_hits_by_chunk_id: dict[UUID, int] = field(default_factory=dict)

    def to_chunk_candidates(
        self,
        max_candidates: int,
    ) -> list[CandidateKnowledgeChunk]:
        """
        Adaptador para el motor chunk: un candidato por fragmento interno o
        registro OAI. Se acota por huellas raras compartidas porque el
        motor chunk compara cada fragmento del documento contra cada
        candidato y no escala a miles.
        """
        ranked: list[tuple[int, CandidateKnowledgeChunk]] = []

        for source in self.sources:
            if source.kind == SOURCE_KIND_OAI and source.oai_record is not None:
                record = source.oai_record
                ranked.append(
                    (
                        source.shared_rare_hashes,
                        CandidateKnowledgeChunk(
                            document_id=record.id,
                            title=record.title,
                            owner_name=record.repository_name,
                            text_excerpt=record.text_excerpt,
                            normalized_text=record.normalized_text,
                        ),
                    )
                )
                continue

            for chunk in source.chunks:
                ranked.append(
                    (
                        self.rare_hits_by_chunk_id.get(chunk.id, 0),
                        CandidateKnowledgeChunk(
                            document_id=source.source_id,
                            title=source.title,
                            owner_name=source.owner_name,
                            text_excerpt=chunk.text_excerpt,
                            normalized_text=chunk.normalized_text,
                        ),
                    )
                )

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in ranked[:max_candidates]]

    def to_span_sources(self) -> list[SpanSource]:
        span_sources: list[SpanSource] = []

        for source in self.sources:
            if source.kind == SOURCE_KIND_OAI and source.oai_record is not None:
                record = source.oai_record
                span_sources.append(
                    SpanSource(
                        key=f"oai:{record.id}",
                        kind=SOURCE_KIND_OAI,
                        title=record.title,
                        owner_name=record.repository_name,
                        url=record.source_url,
                        domain=record.repository_name,
                        source_id=record.id,
                        # Las huellas OAI se calcularon sobre "título +
                        # resumen"; el título va como tramo aparte para no
                        # inventar un tramo que cruce de uno al otro.
                        segments=tuple(
                            segment
                            for segment in (record.title, record.text_excerpt)
                            if segment
                        ),
                    )
                )
                continue

            span_sources.append(
                SpanSource(
                    key=f"internal:{source.source_id}",
                    kind=SOURCE_KIND_INTERNAL,
                    title=source.title,
                    owner_name=source.owner_name,
                    domain="Repositorio interno",
                    source_id=source.source_id,
                    segments=tuple(_merge_chunk_texts(source.chunks)),
                )
            )

        return span_sources


def _merge_chunk_texts(chunks: list[DocumentKnowledgeChunk]) -> list[str]:
    """
    Reconstruye el texto de un documento interno a partir de sus fragmentos
    solapados (85 palabras, paso 45): los que se pisan se cosen por offset
    en un solo tramo; los que no, quedan como tramos separados.
    """
    segments: list[str] = []
    current = ""
    current_end = -1

    for chunk in sorted(chunks, key=lambda item: item.start_offset):
        text = chunk.text_excerpt

        if current and chunk.start_offset <= current_end:
            overlap = current_end - chunk.start_offset

            if chunk.end_offset > current_end:
                current += text[overlap:]
                current_end = chunk.end_offset
            continue

        if current:
            segments.append(current)

        current = text
        current_end = chunk.end_offset

    if current:
        segments.append(current)

    return segments


class CandidateRetriever:
    """
    Recuperación de candidatos sobre el índice de huellas (Winnowing,
    k=4, w=4: garantiza al menos una huella común en toda coincidencia
    idéntica de 7 palabras o más).

    A diferencia de la recuperación vieja (top-40 fragmentos por cantidad
    de huellas compartidas):
    - Descarta las huellas comunes (compartidas por más de
      `max_document_frequency` fuentes): son frases genéricas que acaparan
      el ranking sin indicar copia.
    - Agrupa los fragmentos internos por documento, para que una tesis
      copiada entera no quede limitada a 40 fragmentos.
    - Aplica los filtros (institución, activo, otro documento, otro dueño,
      OAI habilitado) ANTES del corte, para no gastar cupos en candidatos
      que después se descartan.
    """

    def __init__(
        self,
        max_document_frequency: int | None = None,
        max_candidates: int | None = None,
    ) -> None:
        self.max_document_frequency = (
            max_document_frequency
            if max_document_frequency is not None
            else getattr(settings, "SIMILARITY_MAX_DOCUMENT_FREQUENCY", 50)
        )
        self.max_candidates = (
            max_candidates
            if max_candidates is not None
            else getattr(settings, "SIMILARITY_MAX_CANDIDATES", 200)
        )
        self.normalizer = InternalSimilarityEngine()

    def retrieve(
        self,
        document: Document,
        analysis_content: str,
        excluded_ranges: Sequence[tuple[int, int]] = (),
    ) -> RetrievalResult:
        fingerprints = compute_query_fingerprints(
            content=analysis_content,
            excluded_ranges=excluded_ranges,
            normalize=self.normalizer._normalize,
        )

        if not fingerprints:
            return RetrievalResult(sources=[], oai_records_by_id={})

        rare_hashes = self._rare_hashes(fingerprints)
        oai_enabled = getattr(settings, "OAI_HARVEST_ENABLED", True)

        hashes_by_chunk: dict[UUID, set[int]] = defaultdict(set)
        hashes_by_oai: dict[UUID, set[int]] = defaultdict(set)

        for batch in _batches(sorted(rare_hashes)):
            postings = DocumentFingerprint.objects.filter(hash__in=batch)

            if not oai_enabled:
                postings = postings.exclude(source_type="oai")

            for hash_value, source_type, source_id in postings.values_list(
                "hash",
                "source_type",
                "source_id",
            ):
                if source_type == "internal":
                    hashes_by_chunk[source_id].add(hash_value)
                else:
                    hashes_by_oai[source_id].add(hash_value)

        chunk_document_ids = self._allowed_chunk_documents(
            document=document,
            chunk_ids=list(hashes_by_chunk),
        )
        active_oai_ids = self._active_oai_ids(list(hashes_by_oai))

        hashes_by_source: dict[tuple[str, UUID], set[int]] = defaultdict(set)

        for chunk_id, document_id in chunk_document_ids.items():
            hashes_by_source[(SOURCE_KIND_INTERNAL, document_id)].update(
                hashes_by_chunk[chunk_id]
            )

        for record_id in active_oai_ids:
            hashes_by_source[(SOURCE_KIND_OAI, record_id)] = hashes_by_oai[record_id]

        ranked = sorted(
            hashes_by_source.items(),
            key=lambda item: (len(item[1]), str(item[0][1])),
            reverse=True,
        )[: self.max_candidates]

        selected_documents = {
            source_id
            for (kind, source_id), _ in ranked
            if kind == SOURCE_KIND_INTERNAL
        }
        selected_oai = [
            source_id for (kind, source_id), _ in ranked if kind == SOURCE_KIND_OAI
        ]

        chunks_by_document = self._load_chunks(
            chunk_ids=[
                chunk_id
                for chunk_id, document_id in chunk_document_ids.items()
                if document_id in selected_documents
            ]
        )
        oai_records_by_id = self._load_oai_records(selected_oai)

        sources: list[RetrievedSource] = []

        for (kind, source_id), shared in ranked:
            if kind == SOURCE_KIND_OAI:
                record = oai_records_by_id.get(source_id)

                if record is None:
                    continue

                sources.append(
                    RetrievedSource(
                        kind=kind,
                        source_id=source_id,
                        shared_rare_hashes=len(shared),
                        title=record.title,
                        owner_name=record.repository_name,
                        oai_record=record,
                    )
                )
                continue

            chunks = chunks_by_document.get(source_id, [])

            if not chunks:
                continue

            owner = chunks[0].document.owner
            sources.append(
                RetrievedSource(
                    kind=kind,
                    source_id=source_id,
                    shared_rare_hashes=len(shared),
                    title=chunks[0].document.title,
                    owner_name=owner.get_full_name() or owner.username,
                    chunks=chunks,
                )
            )

        logger.info(
            "Recuperación de candidatos. document_id=%s huellas=%s raras=%s "
            "fragmentos_internos=%s registros_oai=%s fuentes=%s",
            document.id,
            len(fingerprints),
            len(rare_hashes),
            len(chunk_document_ids),
            len(active_oai_ids),
            len(sources),
        )

        rare_hits_by_chunk_id = {
            chunk_id: len(hashes_by_chunk[chunk_id])
            for chunk_id in chunk_document_ids
        }

        return RetrievalResult(
            sources=sources,
            oai_records_by_id=oai_records_by_id,
            query_fingerprint_count=len(fingerprints),
            rare_fingerprint_count=len(rare_hashes),
            rare_hits_by_chunk_id=rare_hits_by_chunk_id,
        )

    def _rare_hashes(self, fingerprints: set[int]) -> set[int]:
        """
        Huellas cuya documento-frecuencia (fuentes que la contienen) no
        supera el umbral. Las huellas que no aparecen en el índice no se
        devuelven: no aportan candidatos.
        """
        rare: set[int] = set()

        for batch in _batches(sorted(fingerprints)):
            rows = (
                DocumentFingerprint.objects.filter(hash__in=batch)
                .values("hash")
                .annotate(frequency=Count("id"))
            )

            for row in rows:
                if row["frequency"] <= self.max_document_frequency:
                    rare.add(row["hash"])

        return rare

    def _allowed_chunk_documents(
        self,
        document: Document,
        chunk_ids: list[UUID],
    ) -> dict[UUID, UUID]:
        allowed: dict[UUID, UUID] = {}

        for batch in _batches(chunk_ids):
            rows = (
                DocumentKnowledgeChunk.objects.filter(
                    id__in=batch,
                    is_active=True,
                    document__institution=document.institution,
                )
                .exclude(document=document)
                # Igual que la recuperación vieja: un alumno nunca aparece
                # marcado contra su propio trabajo anterior.
                .exclude(document__owner=document.owner)
                .values_list("id", "document_id")
            )
            allowed.update(rows)

        return allowed

    def _active_oai_ids(self, record_ids: list[UUID]) -> list[UUID]:
        active: list[UUID] = []

        for batch in _batches(record_ids):
            active.extend(
                OaiRecord.objects.filter(id__in=batch, is_active=True).values_list(
                    "id",
                    flat=True,
                )
            )

        return active

    def _load_chunks(
        self,
        chunk_ids: list[UUID],
    ) -> dict[UUID, list[DocumentKnowledgeChunk]]:
        chunks_by_document: dict[UUID, list[DocumentKnowledgeChunk]] = defaultdict(
            list
        )

        for batch in _batches(chunk_ids):
            chunks = DocumentKnowledgeChunk.objects.select_related(
                "document",
                "document__owner",
            ).filter(id__in=batch)

            for chunk in chunks:
                chunks_by_document[chunk.document_id].append(chunk)

        for chunks in chunks_by_document.values():
            chunks.sort(key=lambda chunk: chunk.start_offset)

        return chunks_by_document

    def _load_oai_records(self, record_ids: list[UUID]) -> dict[UUID, OaiRecord]:
        records: dict[UUID, OaiRecord] = {}

        for batch in _batches(record_ids):
            for record in OaiRecord.objects.filter(id__in=batch):
                records[record.id] = record

        return records


def _batches(values: list, size: int = QUERY_BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]
