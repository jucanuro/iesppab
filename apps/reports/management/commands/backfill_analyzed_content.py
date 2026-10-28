from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from apps.reports.models import AnalysisReport
from apps.reports.services import resolve_analyzed_content

PROGRESS_EVERY = 25


class Command(BaseCommand):
    help = (
        "Congela `AnalysisReport.analyzed_content` en los reportes antiguos "
        "que lo tienen vacío, guardando el texto filtrado que hoy se "
        "reconstruye al vuelo (filtro ACTUAL sobre el texto extraído). "
        "Ejecutarlo ANTES de cambiar el extractor o el filtro académico: "
        "así los offsets de los hallazgos de esos reportes siguen calzando "
        "aunque el filtro o el texto extraído cambien después. Por defecto "
        "es un dry-run; usar --apply para escribir. Es idempotente: solo "
        "toca reportes con `analyzed_content` vacío."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Escribe `analyzed_content` en la base de datos. Sin esta "
                "opción solo se muestra qué reportes se congelarían."
            ),
        )

    def handle(self, *args, **options) -> None:
        apply_changes = options["apply"]
        start = time.monotonic()

        reports = (
            AnalysisReport.objects.filter(analyzed_content="")
            .select_related("document", "document__extracted_text")
            .order_by("created_at")
        )

        total = reports.count()
        frozen = 0
        skipped_no_text = 0
        seen = 0

        mode_label = "APLICANDO" if apply_changes else "DRY-RUN (sin escritura)"
        self.stdout.write(
            f"{mode_label}: {total} reportes sin analyzed_content."
        )

        for report in reports.iterator():
            seen += 1
            content = resolve_analyzed_content(report)

            if not content:
                skipped_no_text += 1
                self.stdout.write(
                    f"  SALTADO (sin texto extraído): reporte {report.id} — "
                    f"documento {report.document_id}"
                )
                continue

            if apply_changes:
                # Se filtra otra vez por vacío: si un análisis terminó entre
                # medio y ya guardó su propio texto, no se pisa.
                AnalysisReport.objects.filter(
                    pk=report.pk,
                    analyzed_content="",
                ).update(analyzed_content=content)
                verb = "CONGELADO"
            else:
                verb = "CONGELARÍA"

            frozen += 1
            self.stdout.write(
                f"  {verb}: reporte {report.id} — {report.document.title!r} "
                f"→ {len(content)} caracteres"
            )

            if seen % PROGRESS_EVERY == 0:
                self.stdout.write(f"  ... {seen}/{total} procesados")

        elapsed = time.monotonic() - start
        verb = "congelados" if apply_changes else "se congelarían"
        summary = (
            f"Listo en {elapsed:.1f}s. {frozen} reportes {verb}, "
            f"{skipped_no_text} saltados por no tener texto "
            f"(de {total} reportes sin analyzed_content)."
        )

        if not apply_changes:
            summary += " No se escribió nada (usar --apply para escribir)."

        self.stdout.write(self.style.SUCCESS(summary))
