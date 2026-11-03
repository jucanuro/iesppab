from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


PERCENTAGE_VALIDATORS = [
    MinValueValidator(Decimal("0.00")),
    MaxValueValidator(Decimal("100.00")),
]

# Aviso que acompaña siempre al % de IA (reporte web, PDF, certificado).
AI_ESTIMATE_DISCLAIMER = (
    "Estimación indicativa. La detección de IA no es concluyente y no "
    "constituye prueba; debe ser revisada por el docente."
)


# Escala de colores del índice de similitud (la de Turnitin).
SIMILARITY_BANDS = {
    "blue": {"label": "Sin coincidencias", "bg": "#DBEAFE", "fg": "#1E3A8A", "border": "#93C5FD"},
    "green": {"label": "Similitud baja", "bg": "#D1FAE5", "fg": "#064E3B", "border": "#6EE7B7"},
    "yellow": {"label": "Similitud media", "bg": "#FEF9C3", "fg": "#713F12", "border": "#FDE047"},
    "orange": {"label": "Similitud alta", "bg": "#FFEDD5", "fg": "#7C2D12", "border": "#FDBA74"},
    "red": {"label": "Similitud muy alta", "bg": "#FEE2E2", "fg": "#7F1D1D", "border": "#FCA5A5"},
}
SIMILARITY_LEGEND = [
    ("blue", "0 %"),
    ("green", "1-24 %"),
    ("yellow", "25-49 %"),
    ("orange", "50-74 %"),
    ("red", "75-100 %"),
]


def similarity_band(percent) -> dict:
    value = Decimal(str(percent))
    if value <= 0:
        key = "blue"
    elif value < 25:
        key = "green"
    elif value < 50:
        key = "yellow"
    elif value < 75:
        key = "orange"
    else:
        key = "red"
    return {"key": key, **SIMILARITY_BANDS[key]}


def ai_classifier_band(percent) -> dict:
    value = Decimal(str(percent))
    if value < 20:
        return {"key": "low", "label": "Sin indicios", "bg": "#D1FAE5", "fg": "#064E3B", "border": "#6EE7B7"}
    if value < 50:
        return {"key": "mild", "label": "Indicios leves", "bg": "#FEF9C3", "fg": "#713F12", "border": "#FDE047"}
    return {"key": "high", "label": "Posible IA: revisar", "bg": "#FEE2E2", "fg": "#7F1D1D", "border": "#FCA5A5"}


AI_CLASSIFIER_NOTE = "Estimación indicativa, no es prueba."


class ReportRiskLevel(models.TextChoices):
    LOW = "LOW", _("Bajo")
    MEDIUM = "MEDIUM", _("Medio")
    HIGH = "HIGH", _("Alto")
    CRITICAL = "CRITICAL", _("Crítico")


class AnalysisReport(TimeStampedModel):
    """
    Resultado final consolidado del análisis.
    """

    AI_DISCLAIMER = AI_ESTIMATE_DISCLAIMER
    AI_CLASSIFIER_NOTE = AI_CLASSIFIER_NOTE

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
    analyzed_content = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Texto analizado (filtrado)"),
        help_text=_(
            "Texto sobre el que se calcularon los offsets de los hallazgos "
            "(sin portada/índice/bibliografía). El visor y el PDF señalado "
            "pintan los resaltados sobre este texto, no sobre el crudo, "
            "para que coincidan exactamente."
        ),
    )
    span_alignment = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Alineación del motor span"),
        help_text=_(
            "Cobertura de cada fuente sobre las palabras del texto analizado, "
            "antes de repartirlas. Permite excluir una fuente y recalcular "
            "sin releer las fuentes ni volver a consultar internet. Vacío en "
            "reportes del motor chunk o anteriores a esta función."
        ),
    )
    original_similarity_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=PERCENTAGE_VALIDATORS,
        verbose_name=_("Similitud original"),
        help_text=_("Porcentaje de similitud antes de excluir fuentes."),
    )
    exclude_quotes = models.BooleanField(
        default=True,
        verbose_name=_("Excluir citas textuales"),
        help_text=_(
            "Si las citas cortas entre comillas se descontaron del % de "
            "similitud. Los reportes previos a esta opción quedan en False "
            "porque se calcularon sin excluirlas."
        ),
    )
    exclude_bibliography = models.BooleanField(
        default=True,
        verbose_name=_("Excluir bibliografía y anexos"),
    )
    engine_version = models.CharField(
        max_length=80,
        default="vql-mvp-1.0.0",
        verbose_name=_("Versión del motor"),
    )
    ai_detector = models.CharField(
        max_length=40,
        blank=True,
        default="",
        verbose_name=_("Detector de IA utilizado"),
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

    @property
    def allows_source_exclusion(self) -> bool:
        return bool(self.span_alignment)

    @property
    def excluded_options_label(self) -> str:
        """
        Qué se excluyó del % de similitud según las opciones GUARDADAS del
        análisis ("citas, bibliografía"); vacío si no se excluyó nada.
        """
        excluded = []

        if self.exclude_quotes:
            excluded.append("citas")

        if self.exclude_bibliography:
            excluded.append("bibliografía")

        return ", ".join(excluded)

    @property
    def excluded_quote_ranges(self) -> list[tuple[int, int]]:
        """
        Rangos de `analyzed_content` descontados del % por ser citas. El
        visor y el PDF no pintan similitud dentro de ellos.
        """
        if not self.exclude_quotes:
            return []

        return [
            (int(start), int(end))
            for start, end in (self.summary or {}).get("excluded_quote_ranges", [])
        ]

    @property
    def common_text_ranges(self) -> list[tuple[int, int]]:
        """
        Rangos de `analyzed_content` marcados como texto común: no suman al %
        y el visor y el PDF los muestran en gris (salvo que otra fuente los
        cuente dentro de un tramo propio).
        """
        return [
            (int(start), int(end))
            for _, _, start, end in (self.span_alignment or {}).get("common_text", [])
        ]

    @property
    def similarity_level(self) -> str:
        if self.similarity_percent < Decimal("15.00"):
            return "bajo"

        if self.similarity_percent < Decimal("30.00"):
            return "moderado"

        return "alto"

    @property
    def similarity_band(self) -> dict:
        """Banda de color del índice de similitud (escala de Turnitin)."""
        return similarity_band(self.similarity_percent)

    @property
    def similarity_legend(self) -> list[tuple[str, str, str]]:
        return [
            (key, label, SIMILARITY_BANDS[key]["bg"]) for key, label in SIMILARITY_LEGEND
        ]

    @property
    def ai_classifier(self) -> dict | None:
        """
        % IA del clasificador (indicativo) con su rango, o None si el reporte
        no se analizó con AI_DETECTION_MODE=classifier o el clasificador falló.
        """
        data = ((self.summary or {}).get("ai_score_breakdown") or {}).get("classifier")
        if not data or data.get("error") or "ai_percent" not in data:
            return None
        percent = Decimal(str(data["ai_percent"]))
        return {"percent": percent, **ai_classifier_band(percent)}

    @property
    def ai_level(self) -> str:
        if self.ai_probability_percent < Decimal("15.00"):
            return "bajo"

        if self.ai_probability_percent < Decimal("30.00"):
            return "moderado"

        return "alto"


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
    source_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Clave de la fuente"),
        help_text=_(
            "Identifica la fuente en la alineación del motor span "
            "(\"internal:<uuid>\", \"oai:<uuid>\", \"web:<url>\")."
        ),
    )
    word_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Palabras atribuidas"),
    )
    is_excluded = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Excluida"),
    )
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="excluded_report_sources",
        null=True,
        blank=True,
        verbose_name=_("Excluida por"),
    )
    excluded_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Excluida el"),
    )
    exclusion_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Motivo de exclusión"),
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

    @property
    def resubmission(self) -> dict:
        """
        Datos del posible reenvío del mismo trabajo (dueño y fecha del
        original), o vacío si la fuente no lo es.
        """
        return (self.metadata or {}).get("resubmission") or {}

    @property
    def is_possible_resubmission(self) -> bool:
        return bool(self.resubmission)

    # Los registros OAI se guardan con source_type INTERNAL (ambos motores
    # los tratan junto con los documentos internos); se distinguen por
    # metadata["source"], así que estas propiedades sirven también para
    # reportes anteriores.
    @property
    def is_web(self) -> bool:
        return self.source_type == SourceType.WEB

    @property
    def is_oai(self) -> bool:
        return (self.metadata or {}).get("source") == "oai"

    @property
    def display_title(self) -> str:
        """
        Nombre principal: título de la tesis, documento o página.
        """
        return self.title or self.domain or "Fuente"

    @property
    def display_subtitle(self) -> str:
        """
        Repositorio o dominio. Para documentos internos nunca se muestra
        el nombre del alumno, solo el origen.
        """
        if self.is_web:
            return self.domain or "Internet"

        if self.is_oai:
            return (
                (self.metadata or {}).get("repository_name")
                or self.domain
                or "Repositorio académico"
            )

        return "Repositorio interno"

    @property
    def link_url(self) -> str:
        """
        Enlace público de la fuente: página web, o registro OAI (o su PDF
        si el registro no trae URL). Los documentos internos son privados.
        """
        if self.is_web:
            return self.url

        if self.is_oai:
            return self.url or (self.metadata or {}).get("pdf_url", "")

        return ""


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