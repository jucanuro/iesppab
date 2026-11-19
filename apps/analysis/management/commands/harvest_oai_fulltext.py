from __future__ import annotations

import re
import time
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.analysis.engines.alicia import (
    ALICIA_REPOSITORY,
    ALICIA_SUFFIX,
    AliciaFullTextHarvester,
)
from apps.analysis.engines.oai_fulltext import (
    OaiFullTextHarvester,
    RepositoryThrottled,
    RobotsUnavailable,
)
from apps.analysis.engines.oai_harvester import OaiRepositoryConfig
from apps.analysis.models import (
    DocumentFingerprint,
    OaiFullText,
    OaiFullTextChunk,
    OaiFullTextStatus,
    OaiRecord,
    SOURCE_TYPE_OAI_FULLTEXT,
)

MAX_ATTEMPTS = 3
MAX_CONSECUTIVE_ERRORS = 5
# Página con redirección, GetRecord y PDF con redirección, a 3 s cada una.
ESTIMATED_SECONDS_PER_RECORD = 15


class Command(BaseCommand):
    help = (
        "Descarga el PDF de los registros OAI de un repositorio y guarda su "
        "texto en bloques con huellas, sin conservar el PDF. Es reanudable: "
        "--limite es el total deseado, y al repetir el mismo comando sigue "
        "donde quedó, reintenta los errores hasta 3 veces y los embargos "
        "ya vencidos."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--repositorio",
            required=True,
            help=(
                "Parte del nombre o dominio en OAI_HARVEST_REPOSITORIES, o "
                "ALICIA para los registros guardados con harvest_alicia."
            ),
        )
        parser.add_argument(
            "--limite",
            type=int,
            required=True,
            help="Total de registros procesados que se quiere alcanzar.",
        )
        parser.add_argument(
            "--filtro-titulo",
            default="",
            help="Expresión regular sobre el título (sin distinguir mayúsculas).",
        )
        parser.add_argument(
            "--excluir-titulo",
            default="",
            help="Expresión regular de títulos que se dejan fuera.",
        )
        parser.add_argument(
            "--excluir-id",
            nargs="+",
            default=[],
            help=(
                "Identificadores OAI que no deben entrar al corpus (p. ej. "
                "tesis de control). Falla si alguno no existe o ya está."
            ),
        )
        parser.add_argument(
            "--reintentar-sin-pdf",
            action="store_true",
            help=(
                "Vuelve a procesar los registros marcados sin_pdf (p. ej. tras "
                "mejorar cómo se encuentran los archivos)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Muestra qué se procesaría, sin red y sin escribir nada.",
        )

    def handle(self, *args, **options) -> None:
        repository = self._repository(options["repositorio"])
        title_filter = options["filtro_titulo"]
        title_exclusion = options["excluir_titulo"]
        excluded_ids = options["excluir_id"]

        for pattern in (title_filter, title_exclusion):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise CommandError(f"Expresión inválida {pattern!r}: {exc}") from exc

        self._check_excluded_ids(excluded_ids)

        queryset = self._queryset(repository, title_filter).exclude(
            oai_identifier__in=excluded_ids,
        )

        if title_exclusion:
            queryset = queryset.exclude(title__iregex=title_exclusion)

        retryable = Q(
            full_text__status=OaiFullTextStatus.ERROR,
            full_text__attempts__lt=MAX_ATTEMPTS,
        )
        embargo_expired = Q(
            full_text__status=OaiFullTextStatus.EMBARGADO,
            full_text__embargo_end__lte=timezone.localdate(),
        )
        pending_filter = Q(full_text__isnull=True) | retryable | embargo_expired

        if options["reintentar_sin_pdf"]:
            pending_filter |= Q(full_text__status=OaiFullTextStatus.SIN_PDF)

        pending = queryset.filter(pending_filter)
        finished_count = queryset.count() - pending.count()
        quota = max(options["limite"] - finished_count, 0)
        records = list(pending.order_by("oai_identifier")[:quota])

        if self._is_alicia(repository):
            records = self._interleave_by_domain(records)

        self.stdout.write(
            f"{repository.name}: {queryset.count()} registros, "
            f"{finished_count} terminados, {len(records)} por procesar en "
            "esta corrida."
        )

        if options["dry_run"]:
            hours = len(records) * ESTIMATED_SECONDS_PER_RECORD / 3600
            self.stdout.write(f"DRY-RUN: tiempo estimado {hours:.1f} h.")

            for record in records[:10]:
                self.stdout.write(f"  {record.oai_identifier} {record.title[:70]}")

            return

        self._run(repository, records)
        self._print_totals(queryset)

    def _run(
        self,
        repository: OaiRepositoryConfig,
        records: list[OaiRecord],
    ) -> None:
        if self._is_alicia(repository):
            self._run_alicia(records)
            return

        harvester = OaiFullTextHarvester(repository=repository)
        consecutive_errors = 0
        started_at = time.monotonic()

        for index, record in enumerate(records, start=1):
            try:
                full_text = harvester.harvest(record)
            except (RepositoryThrottled, RobotsUnavailable) as exc:
                raise CommandError(
                    f"Cosecha detenida en {record.oai_identifier}: {exc} "
                    "Lo procesado queda guardado; repite el comando para seguir."
                ) from exc

            self.stdout.write(
                f"[{index}/{len(records)}] {full_text.status} "
                f"{full_text.word_count} palabras "
                f"{record.oai_identifier} {full_text.error_message[:100]}"
            )
            self.stdout.flush()

            if full_text.status == OaiFullTextStatus.ERROR:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                raise CommandError(
                    f"{MAX_CONSECUTIVE_ERRORS} errores seguidos: cosecha "
                    "detenida. Repite el comando cuando el repositorio responda."
                )

        self.stdout.write(
            f"Corrida terminada en {(time.monotonic() - started_at) / 60:.1f} min."
        )

    def _run_alicia(self, records: list[OaiRecord]) -> None:
        """
        Los registros de ALICIA vienen de muchos repositorios: si uno pide
        bajar el ritmo, no tiene robots.txt legible o falla 5 veces seguidas,
        se deja para otra corrida y se sigue con los demás.
        """
        harvester = AliciaFullTextHarvester()
        consecutive_errors: Counter[str] = Counter()
        skipped_domains: dict[str, str] = {}
        started_at = time.monotonic()

        for index, record in enumerate(records, start=1):
            domain = record.repository_domain

            if domain in skipped_domains:
                continue

            try:
                full_text = harvester.harvest(record)
            except (RepositoryThrottled, RobotsUnavailable) as exc:
                skipped_domains[domain] = str(exc)[:150]
                self.stdout.write(f"  {domain} se deja para otra corrida: {exc}")
                continue

            self.stdout.write(
                f"[{index}/{len(records)}] {full_text.status} "
                f"{full_text.word_count} palabras "
                f"{record.oai_identifier} {full_text.error_message[:100]}"
            )
            self.stdout.flush()

            if full_text.status == OaiFullTextStatus.ERROR:
                consecutive_errors[domain] += 1
            else:
                consecutive_errors[domain] = 0

            if consecutive_errors[domain] >= MAX_CONSECUTIVE_ERRORS:
                skipped_domains[domain] = f"{MAX_CONSECUTIVE_ERRORS} errores seguidos"
                self.stdout.write(
                    f"  {domain} se deja para otra corrida: "
                    f"{MAX_CONSECUTIVE_ERRORS} errores seguidos."
                )

        for domain, reason in skipped_domains.items():
            self.stdout.write(f"Pendiente para otra corrida: {domain} ({reason})")

        self.stdout.write(
            f"Corrida terminada en {(time.monotonic() - started_at) / 60:.1f} min."
        )

    def _is_alicia(self, repository: OaiRepositoryConfig) -> bool:
        return repository == ALICIA_REPOSITORY

    def _interleave_by_domain(self, records: list[OaiRecord]) -> list[OaiRecord]:
        """Alterna repositorios para no cargar uno solo durante horas."""
        by_domain: dict[str, list[OaiRecord]] = {}

        for record in records:
            by_domain.setdefault(record.repository_domain, []).append(record)

        queues = list(by_domain.values())
        interleaved: list[OaiRecord] = []

        while queues:
            for queue in list(queues):
                interleaved.append(queue.pop(0))

                if not queue:
                    queues.remove(queue)

        return interleaved

    def _print_totals(self, queryset: QuerySet[OaiRecord]) -> None:
        full_texts = OaiFullText.objects.filter(record__in=queryset)
        statuses = Counter(full_texts.values_list("status", flat=True))
        chunks = OaiFullTextChunk.objects.filter(record__in=queryset).count()
        fingerprints = DocumentFingerprint.objects.filter(
            source_type=SOURCE_TYPE_OAI_FULLTEXT,
        ).count()

        self.stdout.write(f"Estados: {dict(statuses)}")
        self.stdout.write(
            f"Bloques: {chunks}. Huellas de texto completo: {fingerprints}."
        )

    def _check_excluded_ids(self, excluded_ids: list[str]) -> None:
        """
        Un identificador mal escrito dejaría pasar la tesis sin aviso, y una
        de control dentro del corpus invalida la medición.
        """
        existing = set(
            OaiRecord.objects.filter(oai_identifier__in=excluded_ids).values_list(
                "oai_identifier",
                flat=True,
            )
        )
        unknown = sorted(set(excluded_ids) - existing)

        if unknown:
            raise CommandError(f"--excluir-id no existe: {', '.join(unknown)}")

        indexed = sorted(
            OaiFullTextChunk.objects.filter(
                record__oai_identifier__in=excluded_ids,
            )
            .values_list("record__oai_identifier", flat=True)
            .distinct()
        )

        if indexed:
            raise CommandError(f"--excluir-id ya indexado: {', '.join(indexed)}")

    def _queryset(
        self,
        repository: OaiRepositoryConfig,
        title_filter: str,
    ) -> QuerySet[OaiRecord]:
        if self._is_alicia(repository):
            queryset = OaiRecord.objects.filter(
                repository_name__endswith=ALICIA_SUFFIX,
                is_active=True,
            )
        else:
            queryset = OaiRecord.objects.filter(
                repository_domain=repository.domain,
                is_active=True,
            ).exclude(source_url="")

        if title_filter:
            queryset = queryset.filter(title__iregex=title_filter)

        return queryset

    def _repository(self, name: str) -> OaiRepositoryConfig:
        needle = name.lower()

        if needle == "alicia":
            return ALICIA_REPOSITORY

        matches = [
            repository
            for repository in getattr(settings, "OAI_HARVEST_REPOSITORIES", [])
            if needle in repository["name"].lower()
            or needle in repository["domain"].lower()
        ]

        if len(matches) != 1:
            raise CommandError(
                f"--repositorio {name!r} coincide con {len(matches)} "
                "repositorios; debe coincidir con uno."
            )

        return OaiRepositoryConfig(**matches[0])
