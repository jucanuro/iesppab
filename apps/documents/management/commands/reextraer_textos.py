"""
Repara los DocumentText partidos (una palabra por línea) volviendo a extraer
del archivo original con el extractor actual. No reanaliza. Por defecto es un
ensayo (dry-run): solo --apply escribe. Sin dependencias nuevas.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.documents.extractors import (
    DocumentTextExtractor,
    TextExtractionError,
    is_damaged_text,
    text_damage_metrics,
)
from apps.documents.models import DocumentText


def _fmt(metrics: dict) -> str:
    return (
        f"líneas={metrics['lines']} mediana={metrics['median']:g} "
        f"palabras={metrics['words']}"
    )


class Command(BaseCommand):
    help = "Reextrae los textos dañados (palabra por línea). Dry-run por defecto."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--apply", action="store_true", help="Escribe los cambios.")
        parser.add_argument(
            "--umbral", type=float, default=2.0,
            help="Mediana de palabras por línea igual o menor a la cual se repara (2).",
        )
        parser.add_argument("--ids", nargs="+", help="Limita a estos id de documento.")

    def handle(self, *args, **options) -> None:
        apply = options["apply"]
        queryset = DocumentText.objects.select_related("document").order_by(
            "document__title"
        )
        if options["ids"]:
            queryset = queryset.filter(document_id__in=options["ids"])

        extractor = DocumentTextExtractor()
        found = fixed = failed = 0

        for document_text in queryset.iterator(chunk_size=50):
            if not is_damaged_text(document_text.content, options["umbral"]):
                continue
            found += 1
            document = document_text.document
            before = text_damage_metrics(document_text.content)

            if not document.original_file:
                failed += 1
                self.stdout.write(f"{document.id}\t{document.title}\tsin archivo original")
                continue
            try:
                result = extractor.extract(document=document)
            except TextExtractionError as exc:
                failed += 1
                self.stdout.write(f"{document.id}\t{document.title}\tfalló: {exc}")
                continue

            after = text_damage_metrics(result.content)
            self.stdout.write(
                f"{document.id}\t{document.title}\n"
                f"  antes:   {_fmt(before)}\n  después: {_fmt(after)}"
            )
            if is_damaged_text(result.content, options["umbral"]):
                failed += 1
                self.stdout.write("  sigue dañado; no se escribe")
                continue
            if apply:
                extractor.extract_and_save(document=document)
            fixed += 1

        verb = "reparados" if apply else "reparables (dry-run, use --apply)"
        self.stdout.write(self.style.WARNING(
            f"{found} dañados; {fixed} {verb}; {failed} sin reparar."
        ))
