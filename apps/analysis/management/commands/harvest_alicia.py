from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.analysis.engines.alicia import (
    DEFAULT_EXCLUDED_DOMAINS,
    AliciaQuery,
    AliciaSearch,
    save_record,
)
from apps.analysis.engines.oai_fulltext import (
    FullTextFetchError,
    RepositoryThrottled,
    RobotsUnavailable,
)
from apps.analysis.models import OaiRecord


class Command(BaseCommand):
    help = (
        "Guarda metadatos de tesis en español y de acceso abierto desde la API "
        "de ALICIA (CONCYTEC), con los enlaces a sus archivos. No descarga "
        "PDF: eso lo hace luego harvest_oai_fulltext --repositorio ALICIA. "
        "Omite los registros que ya existen (p. ej. los de UPeU)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--limite",
            type=int,
            required=True,
            help="Cantidad de registros nuevos que se quiere guardar en esta corrida.",
        )
        parser.add_argument(
            "--materia",
            default="educación",
            help="Texto buscado en la materia (dc:subject). Vacío = todas.",
        )
        parser.add_argument(
            "--institucion",
            nargs="+",
            default=[],
            help="Siglas de institución en ALICIA (p. ej. EESPPM UMCH UARM).",
        )
        parser.add_argument("--desde-anio", type=int, default=None)
        parser.add_argument("--hasta-anio", type=int, default=None)
        parser.add_argument(
            "--excluir-dominio",
            nargs="*",
            default=list(DEFAULT_EXCLUDED_DOMAINS),
            help=(
                "Dominios de origen que no se guardan (por defecto los que "
                "bloquean la descarga por robots.txt o anti-bots)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo consulta el total en ALICIA (1 petición), sin escribir nada.",
        )

    def handle(self, *args, **options) -> None:
        if options["limite"] <= 0:
            raise CommandError("--limite debe ser mayor que 0.")

        query = AliciaQuery(
            subject=options["materia"].strip(),
            institutions=tuple(options["institucion"]),
            year_from=options["desde_anio"],
            year_to=options["hasta_anio"],
        )
        excluded = {domain.lower() for domain in options["excluir_dominio"]}
        search = AliciaSearch()

        try:
            if options["dry_run"]:
                total = search.total(query)
                hours = min(total, options["limite"]) / query.page_size * 10 / 3600
                self.stdout.write(
                    f"DRY-RUN: ALICIA tiene {total} tesis con ese filtro. "
                    f"Recorrer {options['limite']} toma al menos {hours:.2f} h "
                    "(10 s por página de 100), sin contar los ya existentes."
                )
                return

            self._run(search, query, excluded, options["limite"])
        except (RepositoryThrottled, RobotsUnavailable, FullTextFetchError) as exc:
            raise CommandError(
                f"Cosecha de ALICIA detenida: {exc} Lo guardado se conserva; "
                "repite el comando para seguir."
            ) from exc

    def _run(self, search, query, excluded: set[str], limit: int) -> None:
        counts: Counter[str] = Counter()
        domains: Counter[str] = Counter()
        today = timezone.localdate()

        for record in search.iter_records(query):
            counts["leidos"] += 1

            if record is None:
                counts["incompletos"] += 1
            elif record.domain in excluded:
                counts["dominio_excluido"] += 1
            elif record.embargo_end is not None and record.embargo_end > today:
                counts["embargados"] += 1
            elif OaiRecord.objects.filter(oai_identifier=record.oai_identifier).exists():
                counts["ya_existian"] += 1
            else:
                save_record(record)
                counts["nuevos"] += 1
                domains[record.domain] += 1

            if counts["leidos"] % 100 == 0:
                self.stdout.write(f"  {dict(counts)}")
                self.stdout.flush()

            if counts["nuevos"] >= limit:
                break

        self.stdout.write(self.style.SUCCESS(f"ALICIA: {dict(counts)}"))

        for domain, total in domains.most_common():
            self.stdout.write(f"  {domain}: {total}")
