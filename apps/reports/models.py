from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


PERCENTAGE_VALIDATORS = [
    MinValueValidator(Decimal("0.00")),
    MaxValueValidator(Decimal("100.00")),
]


class ReportRiskLevel(models.TextChoices):
    LOW = "LOW", _("Bajo")
    MEDIUM = "MEDIUM", _("Medio")
    HIGH = "HIGH", _("Alto")
    CRITICAL = "CRITICAL", _("Crítico")


class AnalysisReport(TimeStampedModel):
    """
    Resultado final consolidado del análisis.
    """

    document = models.OneToOneField(
        "documents.Document",
        on_delete=models.CASCADE,
        related_name="report",
        verbose_name=_("Documento"),
    )
    analysis_job = models.OneToOneField(
        "analysis.AnalysisJob",
        on_delete=models.SET_NULL,
        related_name="report",
        null=True,
        blank=True,
        verbose_name=_("Trabajo de análisis"),
    )
    similarity_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Porcentaje de similitud"),
    )
    web_similarity_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Similitud web"),
    )
    internal_similarity_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Similitud interna"),
    )
    ai_probability_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Probabilidad de IA"),
    )
    risk_level = models.CharField(
        max_length=20,
        choices=ReportRiskLevel.choices,
        default=ReportRiskLevel.LOW,
        db_index=True,
        verbose_name=_("Nivel de riesgo"),
    )
    summary = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Resumen técnico"),
    )
    engine_version = models.CharField(
        max_length=80,
        default="vql-mvp-1.0.0",
        verbose_name=_("Versión del motor"),
    )
    generated_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
        verbose_name=_("Generado el"),
    )
    is_final = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Reporte final"),
    )

    class Meta:
        verbose_name = _("Reporte de análisis")
        verbose_name_plural = _("Reportes de análisis")
        ordering = ["-generated_at"]
        indexes = [
            models.Index(fields=["risk_level", "generated_at"]),
            models.Index(fields=["similarity_percent"]),
            models.Index(fields=["ai_probability_percent"]),
        ]

    def __str__(self) -> str:
        return f"Reporte - {self.document.title}"


class SourceType(models.TextChoices):
    WEB = "WEB", _("Fuente web")
    INTERNAL = "INTERNAL", _("Base interna")
    REPOSITORY = "REPOSITORY", _("Repositorio académico")


class ReportSource(TimeStampedModel):
    """
    Fuente encontrada durante el análisis de similitud.
    """

    report = models.ForeignKey(
        AnalysisReport,
        on_delete=models.CASCADE,
        related_name="sources",
        verbose_name=_("Reporte"),
    )
    source_type = models.CharField(
        max_length=20,
        choices=SourceType.choices,
        db_index=True,
        verbose_name=_("Tipo de fuente"),
    )
    title = models.CharField(
        max_length=300,
        verbose_name=_("Título de la fuente"),
    )
    url = models.URLField(
        max_length=1000,
        blank=True,
        verbose_name=_("URL"),
    )
    domain = models.CharField(
        max_length=180,
        blank=True,
        db_index=True,
        verbose_name=_("Dominio"),
    )
    matched_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Porcentaje coincidente"),
    )
    snippet = models.TextField(
        blank=True,
        verbose_name=_("Fragmento coincidente"),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Metadatos"),
    )

    class Meta:
        verbose_name = _("Fuente del reporte")
        verbose_name_plural = _("Fuentes del reporte")
        ordering = ["-matched_percent"]
        indexes = [
            models.Index(fields=["report", "source_type"]),
            models.Index(fields=["domain"]),
        ]

    def __str__(self) -> str:
        return self.title


class FindingType(models.TextChoices):
    SIMILARITY = "SIMILARITY", _("Similitud")
    AI_GENERATED = "AI_GENERATED", _("Posible texto generado por IA")


class ReportFinding(TimeStampedModel):
    """
    Hallazgo puntual para pintar el visor interactivo.

    - SIMILARITY: se mostrará en rojo.
    - AI_GENERATED: se mostrará en azul.
    """

    report = models.ForeignKey(
        AnalysisReport,
        on_delete=models.CASCADE,
        related_name="findings",
        verbose_name=_("Reporte"),
    )
    source = models.ForeignKey(
        ReportSource,
        on_delete=models.SET_NULL,
        related_name="findings",
        null=True,
        blank=True,
        verbose_name=_("Fuente asociada"),
    )
    finding_type = models.CharField(
        max_length=30,
        choices=FindingType.choices,
        db_index=True,
        verbose_name=_("Tipo de hallazgo"),
    )
    start_offset = models.PositiveIntegerField(
        verbose_name=_("Inicio en texto"),
    )
    end_offset = models.PositiveIntegerField(
        verbose_name=_("Fin en texto"),
    )
    text_excerpt = models.TextField(
        verbose_name=_("Texto detectado"),
    )
    confidence_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Confianza"),
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Metadatos técnicos"),
    )

    class Meta:
        verbose_name = _("Hallazgo del reporte")
        verbose_name_plural = _("Hallazgos del reporte")
        ordering = ["start_offset"]
        indexes = [
            models.Index(fields=["report", "finding_type"]),
            models.Index(fields=["start_offset", "end_offset"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_offset__gt=models.F("start_offset")),
                name="finding_end_offset_greater_than_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.finding_type} - {self.confidence_percent}%"