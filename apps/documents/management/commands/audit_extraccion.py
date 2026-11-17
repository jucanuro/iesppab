"""
Auditoría de solo lectura: lista los documentos cuyo texto extraído quedó
partido (mediana de palabras por línea <= umbral), típico de los PDF de
Google Docs que pypdf lee una palabra por línea. No escribe nada en la base
de datos ni en disco, así que es seguro correrlo en producción.
"""

from __future__ import annotations

import statistics

from django.core.management.base import BaseCommand

from apps.documents.models import DocumentText


class Command(BaseCommand):
    help = (
        "Lista los documentos con extracción partida (una palabra por línea). "
        "Solo lectura."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--umbral",
            type=float,
            default=2.0,
            help="Mediana de palabras por línea igual o menor a la cual se lista (2).",
        )

    def handle(self, *args, **options) -> None:
        threshold = options["umbral"]
        affected = 0
        total = 0

        queryset = (
            DocumentText.objects.select_related("document", "document__report")
            .only("content", "document__title", "document__report__summary",
                  "document__report__ai_probability_percent")
            .order_by("document__title")
        )

        self.stdout.write("id\ttítulo\tmediana_palabras_por_línea\t%IA_actual")

        for document_text in queryset.iterator(chunk_size=50):
            total += 1
            counts = [
                len(line.split())
                for line in document_text.content.split("\n")
                if line.strip()
            ]
            if not counts:
                continue
            median = statistics.median(counts)
            if median > threshold:
                continue

            affected += 1
            document = document_text.document
            report = getattr(document, "report", None)
            if report is None:
                ia = "sin reporte"
            elif report.ai_not_evaluable is not None:
                ia = "no evaluable"
            else:
                ia = f"{report.ai_estimated_percent:.1f}%"

            self.stdout.write(
                f"{document.id}\t{document.title}\t{median:g}\t{ia}"
            )

        self.stdout.write(
            self.style.WARNING(
                f"{affected} de {total} documentos con mediana <= {threshold:g}."
            )
        )
