from __future__ import annotations

import logging
from collections import Counter, defaultdict
from uuid import UUID

from django.conf import settings
from django.db.models import Count

from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.engines.similarity import InternalSimilarityEngine
from apps.analysis.engines.span_similarity import (
    SpanAlignment,
    SpanSimilarityEngine,
)
from apps.analysis.models import (
    SOURCE_TYPE_OAI_FULLTEXT,
    DocumentFingerprint,
    DocumentKnowledgeChunk,
    OaiFullTextChunk,
)
from apps.analysis.retrieval import _batches
from apps.documents.models import Document

logger = logging.getLogger(__name__)


class CommonTextFilter:
    """
    Marca como texto común los tramos alineados que no indican copia de una
    fuente concreta: plantillas y fórmulas que repiten muchas tesis. Un tramo
    es común si >= `min_documents` documentos distintos del corpus contienen
    cada uno al menos `min_ratio` de sus huellas.

    Se exige que estén en un mismo documento porque las huellas son de 4
    palabras: el título de una sola tesis puede tener todas sus huellas
    frecuentes sin que nadie más repita el título completo.

    Winnowing elige una huella por ventana mirando solo esa ventana, así que
    las huellas del texto de un tramo son huellas indexadas de cualquier
    documento que contenga ese mismo texto.

    Cuenta como un documento cada registro OAI (resumen y texto completo son
    el mismo) y cada dueño de documentos internos, para que las versiones
    que reenvía un alumno no parezcan documentos distintos. El dueño del
    documento analizado no cuenta, igual que en la recuperación.
    """

    def __init__(
        self,
        min_documents: int | None = None,
        min_ratio: float | None = None,
    ) -> None:
        self.min_documents = (
            min_documents
            if min_documents is not None
            else getattr(settings, "SIMILARITY_COMMON_TEXT_MIN_DOCS", 0)
        )
        self.min_ratio = (
            min_ratio
            if min_ratio is not None
            else getattr(settings, "SIMILARITY_COMMON_TEXT_MIN_RATIO", 0.7)
        )
        self.normalizer = InternalSimilarityEngine()

    @property
    def enabled(self) -> bool:
        return self.min_documents > 0

    def apply(
        self,
        engine: SpanSimilarityEngine,
        content: str,
        alignment: SpanAlignment,
        document: Document,
    ) -> None:
        if not self.enabled:
            return

        hashes_by_run: dict[tuple[int, int], set[int]] = {}

        for aligned in alignment.sources:
            for first, last in engine.coverage_runs(aligned):
                if (first, last) not in hashes_by_run:
                    hashes_by_run[(first, last)] = self._run_hashes(
                        content[alignment.words[first][1] : alignment.words[last][2]]
                    )

        # Una huella con menos filas que `min_documents` no puede estar en
        # tantos documentos: los tramos que no pasan este corte no cargan
        # filas del índice.
        frequent = self._frequent_hashes(
            set().union(*hashes_by_run.values()) if hashes_by_run else set()
        )
        candidates = {
            run: hashes
            for run, hashes in hashes_by_run.items()
            if hashes and len(hashes & frequent) >= self.min_ratio * len(hashes)
        }
        documents_by_hash = self._documents_by_hash(
            set().union(*candidates.values()) if candidates else set(),
            document,
        )
        common_runs = {
            run
            for run, hashes in candidates.items()
            if self._shared_documents(hashes, documents_by_hash) >= self.min_documents
        }

        engine.remove_common_text(
            alignment,
            lambda first, last: (first, last) in common_runs,
        )

        logger.info(
            "Texto común. document_id=%s tramos=%s candidatos=%s comunes=%s "
            "rangos_comunes=%s",
            document.id,
            len(hashes_by_run),
            len(candidates),
            len(common_runs),
            len(alignment.common_runs),
        )

    def _run_hashes(self, text: str) -> set[int]:
        normalized = self.normalizer._normalize(text)

        if len(normalized.split()) < InternalSimilarityEngine.SHINGLE_SIZE:
            return set()

        return compute_fingerprints(normalized)

    def _shared_documents(
        self,
        hashes: set[int],
        documents_by_hash: dict[int, set[tuple[str, UUID]]],
    ) -> int:
        """
        Documentos que contienen al menos `min_ratio` de las huellas.
        """
        hits: Counter[tuple[str, UUID]] = Counter()

        for hash_value in hashes:
            hits.update(documents_by_hash.get(hash_value, ()))

        required = self.min_ratio * len(hashes)

        return sum(1 for count in hits.values() if count >= required)

    def _frequent_hashes(self, hashes: set[int]) -> set[int]:
        frequent: set[int] = set()

        for batch in _batches(sorted(hashes)):
            frequent.update(
                self._postings()
                .filter(hash__in=batch)
                .values("hash")
                .annotate(frequency=Count("id"))
                .filter(frequency__gte=self.min_documents)
                .values_list("hash", flat=True)
            )

        return frequent

    def _postings(self):
        postings = DocumentFingerprint.objects.all()

        if not getattr(settings, "OAI_HARVEST_ENABLED", True):
            postings = postings.exclude(
                source_type__in=("oai", SOURCE_TYPE_OAI_FULLTEXT),
            )

        return postings

    def _documents_by_hash(
        self,
        hashes: set[int],
        document: Document,
    ) -> dict[int, set[tuple[str, UUID]]]:
        documents: dict[int, set[tuple[str, UUID]]] = defaultdict(set)
        block_hashes: dict[UUID, set[int]] = defaultdict(set)
        chunk_hashes: dict[UUID, set[int]] = defaultdict(set)

        for batch in _batches(sorted(hashes)):
            for hash_value, source_type, source_id in (
                self._postings()
                .filter(hash__in=batch)
                .values_list("hash", "source_type", "source_id")
                .iterator()
            ):
                if source_type == "internal":
                    chunk_hashes[source_id].add(hash_value)
                elif source_type == SOURCE_TYPE_OAI_FULLTEXT:
                    block_hashes[source_id].add(hash_value)
                else:
                    documents[hash_value].add(("oai", source_id))

        for batch in _batches(list(block_hashes)):
            for block_id, record_id in OaiFullTextChunk.objects.filter(
                id__in=batch,
            ).values_list("id", "record_id"):
                for hash_value in block_hashes[block_id]:
                    documents[hash_value].add(("oai", record_id))

        for batch in _batches(list(chunk_hashes)):
            for chunk_id, owner_id in (
                DocumentKnowledgeChunk.objects.filter(
                    id__in=batch,
                    is_active=True,
                    document__institution=document.institution,
                )
                .exclude(document__owner=document.owner)
                .values_list("id", "document__owner_id")
            ):
                for hash_value in chunk_hashes[chunk_id]:
                    documents[hash_value].add(("internal", owner_id))

        return documents
