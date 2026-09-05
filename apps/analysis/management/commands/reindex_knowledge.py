from __future__ import annotations

import time
from collections import Counter

from django.core.management.base import BaseCommand

from apps.analysis.engines.text_filters import AcademicTextFilter
from apps.analysis.indexers import DocumentKnowledgeIndexer
from apps.documents.models import Document

PROGRESS_EVERY = 25


class Command(BaseCommand):
    help = (
        "Re-indexa el corpus interno (DocumentKnowledgeChunk + "
        "DocumentFingerprint) aplicando AcademicTextFilter sobre el texto "
        "extraído de cada documento. Repara los fragmentos guardados con "
        "texto crudo (portada/dedicatoria/bibliografía) que contaminaban la "
        "similitud interna. Es idempotente: el indexador borra y recrea los "
        "fragmentos y huellas de cada documento de forma atómica, así que "
        "re-ejecutarlo no duplica nada."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Muestra qué documentos se re-indexarían y cuántos "
                "fragmentos generaría el texto filtrado, sin escribir nada "
                "en la base de datos."
            ),
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        start = time.monotonic()

        text_filter = AcademicTextFilter()
        indexer = DocumentKnowledgeIndexer()

        documents = (
            Document.objects.select_related("extracted_text")
            .order_by("created_at")
        )

        total = documents.count()
        reindexed = 0
        skipped_no_text = 0
        seen = 0

        mode_label = "DRY-RUN (sin escritura)" if dry_run else "RE-INDEXACIÓN"
        self.stdout.write(f"{mode_label}: {total} documentos en total.")

        for document in documents.iterator():
            seen += 1
            document_text = getattr(document, "extracted_text", None)

            if document_text is None or not (document_text.content or "").strip():
                skipped_no_text += 1
                self.stdout.write(
                    f"  SALTADO (sin texto): {document.id} — {document.title!r}"
                )
                continue

            filtered = text_filter.filter_for_similarity(
                content=document_text.content,
            )
            analysis_content = filtered.content or document_text.content

            if dry_run:
                chunk_count = len(indexer._build_chunks(content=analysis_content))
                excluded = dict(Counter(filtered.excluded_sections))
                self.stdout.write(
                    f"  RE-INDEXARÍA: {document.id} — {document.title!r} "
                    f"→ {chunk_count} fragmentos "
                    f"(secciones excluidas: {excluded or 'ninguna'})"
                )
                reindexed += 1
            else:
                chunk_count = indexer.index(
                    document_text=document_text,
                    content=analysis_content,
                )
                self.stdout.write(
                    f"  RE-INDEXADO: {document.id} — {document.title!r} "
                    f"→ {chunk_count} fragmentos"
                )
                reindexed += 1

            if seen % PROGRESS_EVERY == 0:
                self.stdout.write(
                    f"  ... {seen}/{total} procesados "
                    f"({reindexed} re-indexados, {skipped_no_text} saltados)"
                )

        elapsed = time.monotonic() - start
        verb = "se re-indexarían" if dry_run else "re-indexados"

        summary = (
            f"Listo en {elapsed:.1f}s. {reindexed} documentos {verb}, "
            f"{skipped_no_text} saltados por no tener texto "
            f"(de {total} documentos)."
        )

        if dry_run:
            summary += " No se escribió nada (--dry-run)."

        self.stdout.write(self.style.SUCCESS(summary))
