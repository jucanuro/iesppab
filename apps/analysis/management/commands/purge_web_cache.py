from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.analysis.web_cache import purge_expired


class Command(BaseCommand):
    help = (
        "Borra las entradas vencidas de la caché de búsquedas web (Brave) y "
        "de páginas web leídas. Pensado para un timer diario."
    )

    def handle(self, *args, **options) -> None:
        searches, pages = purge_expired()
        self.stdout.write(
            f"Caché web purgada: {searches} búsquedas y {pages} páginas vencidas."
        )
