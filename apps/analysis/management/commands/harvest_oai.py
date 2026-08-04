from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.analysis.engines.oai_harvester import OaiHarvester, OaiRepositoryConfig


class Command(BaseCommand):
    help = (
        "Cosecha metadatos OAI-PMH desde los repositorios configurados en "
        "settings.OAI_HARVEST_REPOSITORIES (ALICIA/CONCYTEC y otros)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--max-pages",
            type=int,
            default=None,
            help=(
                "Límite de páginas ListRecords por repositorio, útil para "
                "pruebas rápidas sin recorrer el repositorio completo."
            ),
        )

    def handle(self, *args, **options) -> None:
        if not getattr(settings, "OAI_HARVEST_ENABLED", True):
            self.stdout.write(
                self.style.WARNING(
                    "OAI_HARVEST_ENABLED=False. No se ejecuta el harvest."
                )
            )
            return

        repositories = getattr(settings, "OAI_HARVEST_REPOSITORIES", [])

        if not repositories:
            self.stdout.write(
                self.style.WARNING(
                    "No hay repositorios configurados en OAI_HARVEST_REPOSITORIES."
                )
            )
            return

        harvester = OaiHarvester(max_pages=options.get("max_pages"))
        total_created = 0
        total_updated = 0

        for repository_data in repositories:
            repository = OaiRepositoryConfig(
                name=repository_data["name"],
                domain=repository_data["domain"],
                base_url=repository_data["base_url"],
                oai_set=repository_data.get("oai_set", ""),
            )

            self.stdout.write(
                f"Cosechando {repository.name} ({repository.base_url})..."
            )

            summary = harvester.harvest_repository(repository=repository)

            total_created += summary.created_count
            total_updated += summary.updated_count

            if summary.error:
                self.stdout.write(
                    self.style.ERROR(f"  Error: {summary.error}")
                )
                continue

            self.stdout.write(
                self.style.SUCCESS(
                    f"  Nuevos: {summary.created_count} | "
                    f"Actualizados: {summary.updated_count} | "
                    f"Omitidos: {summary.skipped_count}"
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Harvest OAI completado. "
                f"Total nuevos: {total_created} | "
                f"Total actualizados: {total_updated}"
            )
        )
