"""Fuente única de verdad para la IA estimada (REQ-CER-06, RN-CER-03).

Certificado, PDF de IA y verificación QR consumen este objeto, de modo que el
porcentaje, la banda, la fecha, la versión y la nota coinciden por construcción.
Versión, detector y fecha se leen SIEMPRE del reporte, nunca de la configuración
actual del motor: un reporte antiguo conserva su versión.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apps.reports.models import (
    AI_NOT_EVALUABLE_LABEL,
    AI_RESPONSIBLE_USE_NOTE,
    AnalysisReport,
    ai_classifier_band,
)


@dataclass(frozen=True)
class AiEstimate:
    percent: Decimal
    band: dict
    generated_at: datetime
    engine_version: str
    ai_detector: str
    method: str
    note: str
    # False cuando el clasificador no pudo medir: no hay % que mostrar.
    evaluable: bool = True
    not_evaluable_note: str = ""
    percent_label: str = ""

    @property
    def band_label(self) -> str:
        return self.band["label"]

    @property
    def version_label(self) -> str:
        if self.ai_detector:
            return f"{self.engine_version} · {self.ai_detector}"
        return self.engine_version


def build_ai_estimate(report: AnalysisReport) -> AiEstimate:
    not_evaluable = report.ai_not_evaluable
    if not_evaluable is not None:
        return AiEstimate(
            percent=Decimal("0.00"),
            band=report.ai_estimated_band,
            generated_at=report.generated_at,
            engine_version=report.engine_version,
            ai_detector=report.ai_detector or "",
            method="Clasificador de IA",
            note=AI_RESPONSIBLE_USE_NOTE,
            evaluable=False,
            not_evaluable_note=not_evaluable["note"],
            percent_label=AI_NOT_EVALUABLE_LABEL,
        )

    classifier = report.ai_classifier
    if classifier is not None:
        percent = classifier["percent"]
        method = "Clasificador de IA"
    else:
        percent = Decimal(str(report.ai_probability_percent))
        method = report.ai_detector or "Detector heurístico"

    return AiEstimate(
        percent=percent,
        band=ai_classifier_band(percent),
        generated_at=report.generated_at,
        engine_version=report.engine_version,
        ai_detector=report.ai_detector or "",
        method=method,
        note=AI_RESPONSIBLE_USE_NOTE,
        percent_label=f"{percent:.1f}%",
    )
