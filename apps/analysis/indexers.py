from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from django.db import transaction

from apps.analysis.models import DocumentKnowledgeChunk
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
    def index(self, document_text: DocumentText) -> int:
        document = document_text.document
        chunks = self._build_chunks(content=document_text.content)

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