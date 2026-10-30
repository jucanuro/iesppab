from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from django.db import transaction

from apps.analysis.engines.fingerprint import (
    compute_fingerprint_positions,
    compute_fingerprints,
)
from apps.analysis.models import (
    SOURCE_TYPE_OAI_FULLTEXT,
    DocumentFingerprint,
    DocumentKnowledgeChunk,
    OaiFullTextChunk,
    OaiRecord,
)
from apps.documents.models import DocumentText


@dataclass(frozen=True)
class ChunkData:
    text_excerpt: str
    normalized_text: str
    start_offset: int
    end_offset: int
    word_count: int
    content_hash: str


class DocumentKnowledgeIndexer:
    """
    Convierte el texto extraído en fragmentos aprendidos.

    Cada documento analizado alimenta la base interna para que futuros
    documentos puedan compararse contra él.
    """

    CHUNK_WORD_SIZE = 85
    CHUNK_STEP = 45
    MIN_WORDS = 35

    @transaction.atomic
    def index(self, document_text: DocumentText, *, content: str) -> int:
        document = document_text.document
        chunks = self._build_chunks(content=content)

        old_chunk_ids = list(
            DocumentKnowledgeChunk.objects.filter(
                document=document,
            ).values_list("id", flat=True)
        )

        DocumentFingerprint.objects.filter(
            source_type="internal",
            source_id__in=old_chunk_ids,
        ).delete()

        DocumentKnowledgeChunk.objects.filter(document=document).delete()

        objects = [
            DocumentKnowledgeChunk(
                document=document,
                text_excerpt=chunk.text_excerpt,
                normalized_text=chunk.normalized_text,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                word_count=chunk.word_count,
                content_hash=chunk.content_hash,
                is_active=True,
            )
            for chunk in chunks
        ]

        DocumentKnowledgeChunk.objects.bulk_create(objects, batch_size=500)

        fingerprint_objects = [
            DocumentFingerprint(
                hash=fingerprint_hash,
                source_type="internal",
                source_id=chunk_object.id,
            )
            for chunk_object in objects
            for fingerprint_hash in compute_fingerprints(
                chunk_object.normalized_text,
            )
        ]

        DocumentFingerprint.objects.bulk_create(
            fingerprint_objects,
            batch_size=500,
            ignore_conflicts=True,
        )

        return len(objects)

    def _build_chunks(self, content: str) -> list[ChunkData]:
        words_with_offsets = self._words_with_offsets(content=content)

        if len(words_with_offsets) < self.MIN_WORDS:
            return []

        chunks: list[ChunkData] = []

        for start_index in range(0, len(words_with_offsets), self.CHUNK_STEP):
            selected_words = words_with_offsets[
                start_index : start_index + self.CHUNK_WORD_SIZE
            ]

            if len(selected_words) < self.MIN_WORDS:
                continue

            start_offset = selected_words[0][1]
            end_offset = selected_words[-1][2]
            text_excerpt = content[start_offset:end_offset].strip()
            normalized_text = self._normalize(text_excerpt)

            if not normalized_text:
                continue

            content_hash = hashlib.sha256(
                normalized_text.encode("utf-8")
            ).hexdigest()

            chunks.append(
                ChunkData(
                    text_excerpt=text_excerpt,
                    normalized_text=normalized_text,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    word_count=len(selected_words),
                    content_hash=content_hash,
                )
            )

        return chunks

    def _words_with_offsets(self, content: str) -> list[tuple[str, int, int]]:
        matches = re.finditer(
            r"\b[a-zA-ZáéíóúÁÉÍÓÚñÑüÜ0-9]+\b",
            content,
            flags=re.UNICODE,
        )

        return [
            (match.group(0), match.start(), match.end())
            for match in matches
        ]

    def _normalize(self, text: str) -> str:
        text = text.lower()
        text = unicodedata.normalize("NFKD", text)
        text = "".join(
            char for char in text if not unicodedata.combining(char)
        )
        text = re.sub(r"[^a-z0-9ñü\s]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


class OaiFullTextIndexer:
    """
    Indexa el texto completo de un registro OAI en bloques contiguos sin
    solape. Cada huella se guarda una sola vez por tesis, en el bloque de su
    primera aparición: así el índice ocupa la mitad que con fragmentos
    solapados y la documento-frecuencia cuenta tesis, no fragmentos. Una
    coincidencia que cruza dos bloques se recupera porque
    `CandidateRetriever` carga también los bloques vecinos.
    """

    BLOCK_WORD_SIZE = 170

    def __init__(self) -> None:
        self.text_indexer = DocumentKnowledgeIndexer()

    @transaction.atomic
    def index(self, record: OaiRecord, *, content: str) -> tuple[int, int]:
        """Devuelve (bloques, huellas) guardados."""
        self.clear(record)

        blocks = self._build_blocks(content=content)
        objects = [
            OaiFullTextChunk(record=record, position=position, text=text)
            for position, text in enumerate(blocks)
        ]
        OaiFullTextChunk.objects.bulk_create(objects, batch_size=500)

        block_starts: list[int] = []
        normalized_blocks: list[str] = []
        word_total = 0

        for text in blocks:
            normalized = self.text_indexer._normalize(text)
            block_starts.append(word_total)
            normalized_blocks.append(normalized)
            word_total += len(normalized.split())

        positions = compute_fingerprint_positions(" ".join(normalized_blocks))
        fingerprint_objects = [
            DocumentFingerprint(
                hash=fingerprint_hash,
                source_type=SOURCE_TYPE_OAI_FULLTEXT,
                source_id=objects[bisect.bisect_right(block_starts, word) - 1].id,
            )
            for fingerprint_hash, word in positions.items()
        ]
        DocumentFingerprint.objects.bulk_create(
            fingerprint_objects,
            batch_size=1000,
            ignore_conflicts=True,
        )

        return len(objects), len(fingerprint_objects)

    def clear(self, record: OaiRecord) -> None:
        chunk_ids = list(
            OaiFullTextChunk.objects.filter(record=record).values_list(
                "id",
                flat=True,
            )
        )
        DocumentFingerprint.objects.filter(
            source_type=SOURCE_TYPE_OAI_FULLTEXT,
            source_id__in=chunk_ids,
        ).delete()
        OaiFullTextChunk.objects.filter(record=record).delete()

    def _build_blocks(self, content: str) -> list[str]:
        words = self.text_indexer._words_with_offsets(content=content)
        starts = [word[1] for word in words[:: self.BLOCK_WORD_SIZE]]

        return [
            content[start:end]
            for start, end in zip(starts, starts[1:] + [len(content)])
        ]
