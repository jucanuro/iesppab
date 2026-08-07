from __future__ import annotations

import time
from typing import Iterable

from django.core.management.base import BaseCommand

from apps.analysis.engines.fingerprint import compute_fingerprints
from apps.analysis.models import DocumentFingerprint, DocumentKnowledgeChunk, OaiRecord

BATCH_SIZE = 500
PROGRESS_EVERY = 5000


class Command(BaseCommand):
    help = (
        "Calcula y guarda DocumentFingerprint (Winnowing) para los "
        "OaiRecord y DocumentKnowledgeChunk existentes. Re-ejecutable "
        "sin duplicar huellas."
    )

    def handle(self, *args, **options) -> None:
        start = time.monotonic()

        oai_processed, oai_fingerprints = self._backfill(
            records=OaiRecord.objects.filter(is_active=True).iterator(
                chunk_size=BATCH_SIZE,
            ),
            source_type="oai",
            label="OaiRecord",
        )

        internal_processed, internal_fingerprints = self._backfill(
            records=DocumentKnowledgeChunk.objects.filter(
                is_active=True,
            ).iterator(chunk_size=BATCH_SIZE),
            source_type="internal",
            label="DocumentKnowledgeChunk",
        )

        elapsed = time.monotonic() - start

        self.stdout.write(
            self.style.SUCCESS(
                "Backfill completo en {elapsed:.1f}s. "
                "OaiRecord: {oai_processed} procesados / {oai_fp} huellas. "
                "DocumentKnowledgeChunk: {internal_processed} procesados / "
                "{internal_fp} huellas.".format(
                    elapsed=elapsed,
                    oai_processed=oai_processed,
                    oai_fp=oai_fingerprints,
                    internal_processed=internal_processed,
                    internal_fp=internal_fingerprints,
                )
            )
        )

    def _backfill(
        self,
        records: Iterable,
        source_type: str,
        label: str,
    ) -> tuple[int, int]:
        buffer: list[DocumentFingerprint] = []
        processed = 0
        total_fingerprints = 0

        for record in records:
            fingerprints = compute_fingerprints(record.normalized_text)
            total_fingerprints += len(fingerprints)
            processed += 1

            buffer.extend(
                DocumentFingerprint(
                    hash=fingerprint_hash,
                    source_type=source_type,
                    source_id=record.id,
                )
                for fingerprint_hash in fingerprints
            )

            if len(buffer) >= BATCH_SIZE:
                DocumentFingerprint.objects.bulk_create(
                    buffer,
                    ignore_conflicts=True,
                )
                buffer.clear()

            if processed % PROGRESS_EVERY == 0:
                self.stdout.write(f"{label}: {processed} procesados...")

        if buffer:
            DocumentFingerprint.objects.bulk_create(
                buffer,
                ignore_conflicts=True,
            )

        self.stdout.write(
            f"{label}: {processed} procesados en total, "
            f"{total_fingerprints} huellas."
        )

        return processed, total_fingerprints
