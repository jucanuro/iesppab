from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.certificates.models import Certificate
from apps.certificates.services import CERTIFICATE_BLOCKING_RISK_LEVELS
from apps.documents.models import DocumentStatus
from apps.reports.models import AnalysisReport, ReportRiskLevel
from apps.reports.services import resolve_risk_level


class Command(BaseCommand):
    help = (
        "Recalcula `AnalysisReport.risk_level` solo desde la similitud "
        "guardada (`similarity_percent`, que ya descuenta las fuentes "
        "excluidas por el docente). Corrige los reportes cuyo riesgo se "
        "calculó cuando el % de IA también lo subía. Por defecto es un "
        "dry-run; usar --apply para escribir (en una sola transacción). "
        "No emite ni revoca certificados."
    )

    def add_arguments(self, parser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Solo muestra qué cambiaría (comportamiento por defecto).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Guarda los nuevos niveles de riesgo en la base de datos.",
        )

    def handle(self, *args, **options) -> None:
        apply_changes = options["apply"]
        labels = dict(ReportRiskLevel.choices)

        reports = AnalysisReport.objects.select_related("document").order_by(
            "created_at"
        )
        certified_ids = set(
            Certificate.objects.values_list("report_id", flat=True)
        )

        changes: list[tuple[AnalysisReport, str]] = []

        for report in reports.iterator():
            new_level = resolve_risk_level(report.similarity_percent)

            if new_level != report.risk_level:
                changes.append((report, new_level))

        mode_label = "APLICANDO" if apply_changes else "DRY-RUN (sin escritura)"
        self.stdout.write(
            f"{mode_label}: {len(changes)} de {reports.count()} reportes "
            "cambian de nivel de riesgo."
        )

        transitions = Counter(
            (report.risk_level, new_level) for report, new_level in changes
        )

        for (old_level, new_level), count in sorted(transitions.items()):
            self.stdout.write(
                f"  {labels[old_level]} → {labels[new_level]}: {count}"
            )

        unblocked = [
            (report, new_level)
            for report, new_level in changes
            if report.risk_level in CERTIFICATE_BLOCKING_RISK_LEVELS
            and new_level not in CERTIFICATE_BLOCKING_RISK_LEVELS
            and report.id not in certified_ids
        ]

        self.stdout.write(
            f"Certificado bloqueado por riesgo que pasaría a permitirse: "
            f"{len(unblocked)}"
        )

        for report, new_level in unblocked:
            pending = []

            if not report.is_final:
                pending.append("reporte no final")

            if report.document.status != DocumentStatus.COMPLETED:
                pending.append("documento no completado")

            note = f" (aún no certificable: {', '.join(pending)})" if pending else ""
            self.stdout.write(
                f"  reporte {report.id} — {report.document.title!r}: "
                f"similitud {report.similarity_percent}% · "
                f"IA {report.ai_probability_percent}% · "
                f"{labels[report.risk_level]} → {labels[new_level]}{note}"
            )

        if not apply_changes:
            self.stdout.write(
                self.style.SUCCESS(
                    "No se escribió nada (usar --apply para escribir)."
                )
            )
            return

        updated = 0

        with transaction.atomic():
            for report, new_level in changes:
                # Se filtra por el nivel leído: si un análisis o una exclusión
                # lo cambió entre medio, no se pisa.
                updated += AnalysisReport.objects.filter(
                    pk=report.pk,
                    risk_level=report.risk_level,
                ).update(risk_level=new_level)

        self.stdout.write(
            self.style.SUCCESS(
                f"Listo. {updated} reportes actualizados. "
                "No se emitió ni revocó ningún certificado."
            )
        )
