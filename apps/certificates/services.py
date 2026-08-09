from __future__ import annotations

import hashlib
import logging
import secrets
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any
from uuid import UUID

import qrcode
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from apps.accounts.models import User
from apps.certificates.models import Certificate
from apps.documents.models import DocumentStatus
from apps.reports.models import AnalysisReport

logger = logging.getLogger(__name__)


class CertificateGenerationError(Exception):
    """
    Error controlado al generar certificado PDF.
    """

    pass


@dataclass(frozen=True)
class CertificatePayload:
    certificate: Certificate
    verification_url: str


class CertificateGenerationService:
    """
    Servicio responsable de generar certificados PDF institucionales
    a partir de un reporte final.
    """

    def __init__(self, issued_by: User, absolute_base_url: str) -> None:
        self.issued_by = issued_by
        self.absolute_base_url = absolute_base_url.rstrip("/")

    @transaction.atomic
    def execute(self, document_id: UUID) -> Certificate:
        report = self._get_allowed_report(document_id=document_id)

        self._validate_report(report=report)

        certificate = self._get_or_create_certificate(report=report)

        certificate.issued_by = self.issued_by
        certificate.is_active = True
        certificate.revoked_at = None
        certificate.revoke_reason = ""
        certificate.save(
            update_fields=[
                "issued_by",
                "is_active",
                "revoked_at",
                "revoke_reason",
                "updated_at",
            ]
        )

        verification_url = self._build_verification_url(
            certificate=certificate,
        )

        pdf_content = self._render_pdf(
            report=report,
            certificate=certificate,
            verification_url=verification_url,
        )

        filename = f"{certificate.code}.pdf"

        certificate.pdf_file.save(
            filename,
            ContentFile(pdf_content),
            save=False,
        )
        certificate.save(update_fields=["pdf_file", "updated_at"])

        logger.info(
            "Certificado PDF generado. certificate_id=%s report_id=%s",
            certificate.id,
            report.id,
        )

        return certificate

    def _get_allowed_report(self, document_id: UUID) -> AnalysisReport:
        queryset = AnalysisReport.objects.select_related(
            "document",
            "document__institution",
            "document__owner",
            "document__uploaded_by",
            "document__advisor",
            "document__extracted_text",
            "analysis_job",
        ).filter(document_id=document_id)

        if self.issued_by.is_student_role:
            queryset = queryset.filter(document__owner=self.issued_by)

        elif not self.issued_by.is_superuser:
            if self.issued_by.institution_id is None:
                raise PermissionDenied(
                    "Tu usuario no tiene institución asignada."
                )

            queryset = queryset.filter(
                document__institution=self.issued_by.institution,
            )

        report = queryset.first()

        if report is None:
            raise PermissionDenied(
                "No tienes permiso para generar este certificado."
            )

        return report

    def _validate_report(self, report: AnalysisReport) -> None:
        if not report.is_final:
            raise ValidationError(
                "Solo se puede certificar un reporte final."
            )

        if report.document.status != DocumentStatus.COMPLETED:
            raise ValidationError(
                "El documento debe estar completado antes de generar certificado."
            )

    def _get_or_create_certificate(
        self,
        report: AnalysisReport,
    ) -> Certificate:
        existing_certificate = Certificate.objects.filter(
            report=report,
        ).first()

        if existing_certificate:
            return existing_certificate

        code = self._generate_certificate_code(
            institution=report.document.institution,
        )
        verification_hash = self._generate_verification_hash(
            report=report,
            code=code,
        )

        return Certificate.objects.create(
            report=report,
            code=code,
            verification_hash=verification_hash,
            issued_by=self.issued_by,
            is_active=True,
        )

    def _generate_certificate_code(self, institution: Any) -> str:
        year = timezone.now().year
        prefix = self._build_certificate_prefix(institution=institution)

        for _ in range(10):
            token = secrets.token_hex(4).upper()
            code = f"{prefix}-{year}-{token}"

            if not Certificate.objects.filter(code=code).exists():
                return code

        raise CertificateGenerationError(
            "No se pudo generar un código único de certificado."
        )

    def _build_certificate_prefix(self, institution: Any) -> str:
        if institution.short_code.strip():
            return self._to_ascii_alnum(institution.short_code)[:15] or "CERT"

        return self._derive_prefix_from_name(institution.name)

    def _derive_prefix_from_name(self, name: str) -> str:
        """
        Deriva un prefijo corto a partir del nombre institucional: toma la
        primera letra de cada palabra alfabética, sin tildes/diacríticos ni
        espacios, en mayúsculas. Ej: "IESPP Alfonso Barrantes Lingán" -> "IABL".
        """
        ascii_name = self._to_ascii_alnum(name, keep_spaces=True)
        words = [word for word in ascii_name.split() if word]
        initials = "".join(word[0] for word in words)

        return initials[:15] or "CERT"

    def _to_ascii_alnum(self, value: str, keep_spaces: bool = False) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")

        allowed = str.isalnum if not keep_spaces else (lambda ch: ch.isalnum() or ch.isspace())

        return "".join(ch for ch in ascii_value.upper() if allowed(ch))

    def _generate_verification_hash(
        self,
        report: AnalysisReport,
        code: str,
    ) -> str:
        raw_value = (
            f"{report.id}:{report.document_id}:{code}:"
            f"{timezone.now().isoformat()}:{secrets.token_hex(16)}"
        )

        return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()

    def _build_verification_url(
        self,
        certificate: Certificate,
    ) -> str:
        path = reverse(
            "certificates:verify",
            kwargs={"verification_hash": certificate.verification_hash},
        )
        return f"{self.absolute_base_url}{path}"

    def _render_pdf(
        self,
        report: AnalysisReport,
        certificate: Certificate,
        verification_url: str,
    ) -> bytes:
        buffer = BytesIO()

        institution = report.document.institution
        owner = report.document.owner
        issued_by = certificate.issued_by
        document = report.document

        self._prepare_letterhead(institution=institution)

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=1.7 * cm,
            leftMargin=1.7 * cm,
            topMargin=1.8 * cm,
            bottomMargin=1.5 * cm,
            title=f"Certificado {certificate.code}",
            author=institution.name,
        )

        story: list[Any] = []
        styles = self._build_styles()

        story.append(
            Paragraph(
                "CERTIFICADO INSTITUCIONAL DE ORIGINALIDAD",
                styles["title"],
            )
        )
        story.append(Spacer(1, 0.25 * cm))

        story.append(
            Paragraph(
                "Sistema privado de integridad académica y control documental",
                styles["subtitle"],
            )
        )
        story.append(Spacer(1, 0.7 * cm))

        header_table = Table(
            [
                [
                    Paragraph(
                        f"<b>Institución:</b><br/>{institution.name}",
                        styles["small"],
                    ),
                    Paragraph(
                        f"<b>Código:</b><br/>{certificate.code}",
                        styles["small"],
                    ),
                    Paragraph(
                        f"<b>Fecha de emisión:</b><br/>"
                        f"{timezone.localtime(certificate.created_at).strftime('%d/%m/%Y %H:%M')}",
                        styles["small"],
                    ),
                ]
            ],
            colWidths=[8.2 * cm, 4.2 * cm, 4.2 * cm],
        )
        header_table.setStyle(self._card_table_style())
        story.append(header_table)
        story.append(Spacer(1, 0.55 * cm))

        intro_text = (
            "Se deja constancia que el documento académico indicado fue "
            f"procesado por la plataforma institucional de {institution.name}, "
            "obteniéndose los resultados técnicos de similitud textual y "
            "estimación de patrones asociados a inteligencia artificial."
        )
        story.append(Paragraph(intro_text, styles["body"]))
        story.append(Spacer(1, 0.5 * cm))

        document_table = Table(
            [
                [
                    Paragraph("<b>Documento</b>", styles["table_header"]),
                    Paragraph(report.document.title, styles["table_value"]),
                ],
                [
                    Paragraph("<b>Autor / Alumno</b>", styles["table_header"]),
                    Paragraph(
                        owner.get_full_name() or owner.username,
                        styles["table_value"],
                    ),
                ],
                [
                    Paragraph("<b>Tipo</b>", styles["table_header"]),
                    Paragraph(
                        report.document.get_kind_display(),
                        styles["table_value"],
                    ),
                ],
                [
                    Paragraph("<b>Archivo original</b>", styles["table_header"]),
                    Paragraph(
                        report.document.original_filename,
                        styles["table_value"],
                    ),
                ],
            ],
            colWidths=[4.3 * cm, 12.3 * cm],
        )
        document_table.setStyle(self._info_table_style())
        story.append(document_table)
        story.append(Spacer(1, 0.55 * cm))

        metrics_table = Table(
            [
                [
                    Paragraph("SIMILITUD", styles["metric_label_red"]),
                    Paragraph("IA ESTIMADA", styles["metric_label_blue"]),
                    Paragraph("RIESGO", styles["metric_label_dark"]),
                ],
                [
                    Paragraph(
                        f"{self._format_decimal(report.similarity_percent)}%",
                        styles["metric_value_red"],
                    ),
                    Paragraph(
                        f"{self._format_decimal(report.ai_probability_percent)}%",
                        styles["metric_value_blue"],
                    ),
                    Paragraph(
                        report.get_risk_level_display().upper(),
                        styles["metric_value_dark"],
                    ),
                ],
            ],
            colWidths=[5.4 * cm, 5.4 * cm, 5.4 * cm],
        )
        metrics_table.setStyle(self._metrics_table_style())
        story.append(metrics_table)
        story.append(Spacer(1, 0.6 * cm))

        technical_table = Table(
            [
                [
                    Paragraph("<b>Similitud web</b>", styles["table_header"]),
                    Paragraph(
                        f"{self._format_decimal(report.web_similarity_percent)}%",
                        styles["table_value"],
                    ),
                ],
                [
                    Paragraph("<b>Similitud interna</b>", styles["table_header"]),
                    Paragraph(
                        f"{self._format_decimal(report.internal_similarity_percent)}%",
                        styles["table_value"],
                    ),
                ],
                [
                    Paragraph("<b>Motor de análisis</b>", styles["table_header"]),
                    Paragraph(report.engine_version, styles["table_value"]),
                ],
                [
                    Paragraph("<b>Reporte generado</b>", styles["table_header"]),
                    Paragraph(
                        timezone.localtime(report.generated_at).strftime(
                            "%d/%m/%Y %H:%M"
                        ),
                        styles["table_value"],
                    ),
                ],
            ],
            colWidths=[4.3 * cm, 12.3 * cm],
        )
        technical_table.setStyle(self._info_table_style())
        story.append(technical_table)
        story.append(Spacer(1, 0.6 * cm))

        sources_section = self._build_sources_section(report=report, styles=styles)
        if sources_section:
            story.extend(sources_section)
            story.append(Spacer(1, 0.6 * cm))

        note = (
            "<b>Nota técnica:</b> Los porcentajes presentados constituyen "
            "indicadores de apoyo para revisión académica. La detección de IA "
            "es una estimación técnica y no representa una prueba concluyente "
            "de autoría automatizada."
        )
        story.append(Paragraph(note, styles["note"]))
        story.append(Spacer(1, 0.7 * cm))

        qr_buffer = self._generate_qr_buffer(verification_url=verification_url)
        qr_image = Image(qr_buffer, width=3.0 * cm, height=3.0 * cm)

        if document.advisor is not None:
            advisor_name = document.advisor.get_full_name() or document.advisor.username
        else:
            advisor_name = "Asesor no asignado"

        footer_table = Table(
            [
                [
                    Paragraph(
                        "<br/><br/>______________________________<br/>"
                        f"<b>{advisor_name}</b><br/>"
                        "Asesor(a) responsable<br/>"
                        f"<font size=6.5 color='#64748B'>Emitido por: "
                        f"{issued_by.get_full_name() or issued_by.username}</font>",
                        styles["signature"],
                    ),
                    qr_image,
                ],
                [
                    Paragraph(
                        f"<b>Hash de verificación:</b><br/>{certificate.verification_hash}",
                        styles["hash"],
                    ),
                    Paragraph(
                        "Escanee el QR para validar la autenticidad.",
                        styles["qr_text"],
                    ),
                ],
            ],
            colWidths=[12.3 * cm, 4.3 * cm],
        )
        footer_table.setStyle(self._footer_table_style())
        story.append(footer_table)

        story.append(PageBreak())
        story.extend(
            self._build_receipt_section(
                document=document,
                institution=institution,
                owner=owner,
                styles=styles,
            )
        )

        doc.build(
            story,
            onFirstPage=self._decorate_page,
            onLaterPages=self._decorate_page,
        )

        return buffer.getvalue()

    def _build_styles(self) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()

        return {
            "title": ParagraphStyle(
                "title",
                parent=base["Title"],
                fontName="Helvetica-Bold",
                fontSize=18,
                leading=22,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#123f9e"),
                spaceAfter=4,
            ),
            "subtitle": ParagraphStyle(
                "subtitle",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9,
                leading=12,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#64748B"),
            ),
            "body": ParagraphStyle(
                "body",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=10,
                leading=15,
                alignment=TA_LEFT,
                textColor=colors.HexColor("#334155"),
            ),
            "small": ParagraphStyle(
                "small",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#334155"),
            ),
            "table_header": ParagraphStyle(
                "table_header",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#475569"),
            ),
            "table_value": ParagraphStyle(
                "table_value",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#0F172A"),
            ),
            "metric_label_red": ParagraphStyle(
                "metric_label_red",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#B91C1C"),
            ),
            "metric_label_blue": ParagraphStyle(
                "metric_label_blue",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#0369A1"),
            ),
            "metric_label_dark": ParagraphStyle(
                "metric_label_dark",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#334155"),
            ),
            "metric_value_red": ParagraphStyle(
                "metric_value_red",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=20,
                leading=24,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#DC2626"),
            ),
            "metric_value_blue": ParagraphStyle(
                "metric_value_blue",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=20,
                leading=24,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#0284C7"),
            ),
            "metric_value_dark": ParagraphStyle(
                "metric_value_dark",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=13,
                leading=18,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#0F172A"),
            ),
            "note": ParagraphStyle(
                "note",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=12,
                textColor=colors.HexColor("#78350F"),
                backColor=colors.HexColor("#FEF3C7"),
                borderColor=colors.HexColor("#F59E0B"),
                borderWidth=0.5,
                borderPadding=8,
            ),
            "signature": ParagraphStyle(
                "signature",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=12,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#334155"),
            ),
            "hash": ParagraphStyle(
                "hash",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=6.5,
                leading=9,
                textColor=colors.HexColor("#64748B"),
            ),
            "qr_text": ParagraphStyle(
                "qr_text",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=7,
                leading=10,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#0F172A"),
            ),
            "sources_title": ParagraphStyle(
                "sources_title",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=11,
                leading=14,
                textColor=colors.HexColor("#123f9e"),
            ),
            "sources_header": ParagraphStyle(
                "sources_header",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=11,
                textColor=colors.white,
            ),
            "sources_cell": ParagraphStyle(
                "sources_cell",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=8,
                leading=11,
                textColor=colors.HexColor("#334155"),
            ),
            "sources_percent": ParagraphStyle(
                "sources_percent",
                parent=base["Normal"],
                fontName="Helvetica-Bold",
                fontSize=8,
                leading=11,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#123f9e"),
            ),
            "receipt_footer": ParagraphStyle(
                "receipt_footer",
                parent=base["Normal"],
                fontName="Helvetica",
                fontSize=9,
                leading=13,
                alignment=TA_CENTER,
                textColor=colors.HexColor("#334155"),
            ),
        }

    def _card_table_style(self) -> TableStyle:
        return TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 8),
            ]
        )

    def _info_table_style(self) -> TableStyle:
        return TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 7),
            ]
        )

    def _metrics_table_style(self) -> TableStyle:
        return TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FEF2F2")),
                ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#ECFEFF")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F8FAFC")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("PADDING", (0, 0), (-1, -1), 9),
            ]
        )

    def _footer_table_style(self) -> TableStyle:
        return TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )

    def _build_sources_section(
        self,
        report: AnalysisReport,
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        sources = list(
            report.sources.order_by("-matched_percent")[:10]
        )

        if not sources:
            return []

        section: list[Any] = []

        accent_bar = Table([[""]], colWidths=[16.6 * cm], rowHeights=[0.12 * cm])
        accent_bar.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5b400")),
                ]
            )
        )
        section.append(accent_bar)
        section.append(Spacer(1, 0.25 * cm))

        section.append(Paragraph("Fuentes detectadas", styles["sources_title"]))
        section.append(Spacer(1, 0.25 * cm))

        rows: list[list[Any]] = [
            [
                Paragraph("Fuente", styles["sources_header"]),
                Paragraph("Descripción", styles["sources_header"]),
                Paragraph("%", styles["sources_header"]),
            ]
        ]

        for source in sources:
            rows.append(
                [
                    Paragraph(
                        source.domain or "—",
                        styles["sources_cell"],
                    ),
                    Paragraph(
                        self._truncate(source.title, 90),
                        styles["sources_cell"],
                    ),
                    Paragraph(
                        f"{self._format_decimal(source.matched_percent)}%",
                        styles["sources_percent"],
                    ),
                ]
            )

        sources_table = Table(
            rows,
            colWidths=[4.5 * cm, 9.6 * cm, 2.5 * cm],
            repeatRows=1,
        )
        sources_table.setStyle(self._sources_table_style())
        section.append(sources_table)

        return section

    def _sources_table_style(self) -> TableStyle:
        return TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#123f9e")),
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#CBD5E1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E2E8F0")),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F0F5FF")],
                ),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (2, 0), (2, -1), "CENTER"),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )

    def _truncate(self, value: str, max_length: int) -> str:
        value = value or ""

        if len(value) <= max_length:
            return value

        return f"{value[: max_length - 1].rstrip()}…"

    def _generate_qr_buffer(self, verification_url: str) -> BytesIO:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(verification_url)
        qr.make(fit=True)

        image = qr.make_image(fill_color="black", back_color="white")

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        return buffer

    def _prepare_letterhead(self, institution: Any) -> None:
        """
        Precalcula nombre y logo institucional para el membrete,
        dibujado en cada página por `_decorate_page`.
        """
        self._letterhead_name = institution.name.upper()
        self._letterhead_logo: ImageReader | None = None

        if not institution.logo:
            return

        try:
            institution.logo.open("rb")
            image_bytes = institution.logo.read()
            self._letterhead_logo = ImageReader(BytesIO(image_bytes))
        except Exception:
            logger.warning(
                "No se pudo cargar el logo institucional para el certificado. "
                "institution_id=%s",
                institution.id,
                exc_info=True,
            )
        finally:
            institution.logo.close()

    def _decorate_page(self, canvas: Any, doc: Any) -> None:
        width, height = A4
        bar_height = 1.0 * cm

        canvas.saveState()

        canvas.setFillColor(colors.HexColor("#123f9e"))
        canvas.rect(0, height - bar_height, width, bar_height, fill=1, stroke=0)

        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.rect(0, 0, width, 0.45 * cm, fill=1, stroke=0)

        text_x = 1.7 * cm
        logo = getattr(self, "_letterhead_logo", None)

        if logo is not None:
            logo_size = bar_height - 0.2 * cm
            canvas.drawImage(
                logo,
                1.4 * cm,
                height - bar_height + 0.1 * cm,
                width=logo_size,
                height=logo_size,
                preserveAspectRatio=True,
                mask="auto",
            )
            text_x = 1.4 * cm + logo_size + 0.3 * cm

        institution_name = getattr(self, "_letterhead_name", "INSTITUCIÓN")

        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(
            text_x,
            height - 0.65 * cm,
            f"{institution_name} - INTEGRIDAD ACADÉMICA",
        )

        canvas.setFont("Helvetica", 6.5)
        canvas.drawRightString(
            width - 1.7 * cm,
            0.17 * cm,
            f"Pagina {doc.page}",
        )

        canvas.restoreState()

    def _build_receipt_section(
        self,
        document: Any,
        institution: Any,
        owner: User,
        styles: dict[str, ParagraphStyle],
    ) -> list[Any]:
        section: list[Any] = []

        section.append(Paragraph("Recibo digital de entrega", styles["title"]))
        section.append(Spacer(1, 0.2 * cm))
        section.append(
            Paragraph(
                "Comprobante de procesamiento del documento académico",
                styles["subtitle"],
            )
        )
        section.append(Spacer(1, 0.7 * cm))

        extracted_text = getattr(document, "extracted_text", None)
        word_count = (
            f"{extracted_text.word_count:,}".replace(",", " ")
            if extracted_text is not None
            else "No disponible"
        )

        advisor_display = (
            document.advisor.get_full_name() or document.advisor.username
            if document.advisor is not None
            else "No asignado"
        )

        rows = [
            ("Autor de la entrega", owner.get_full_name() or owner.username),
            ("Asesor", advisor_display),
            ("Título de la entrega", document.title),
            ("Nombre del archivo original", document.original_filename),
            ("Tamaño del archivo", self._format_file_size(document.file_size_bytes)),
            ("Total de palabras", word_count),
            (
                "Fecha de entrega",
                timezone.localtime(document.created_at).strftime("%d/%m/%Y %H:%M"),
            ),
            ("Identificador de la entrega", str(document.id)),
        ]

        receipt_table = Table(
            [
                [
                    Paragraph(f"<b>{label}</b>", styles["table_header"]),
                    Paragraph(value, styles["table_value"]),
                ]
                for label, value in rows
            ],
            colWidths=[5.5 * cm, 11.1 * cm],
        )
        receipt_table.setStyle(self._info_table_style())
        section.append(receipt_table)
        section.append(Spacer(1, 0.8 * cm))

        section.append(
            Paragraph(
                "Este recibo confirma que el documento fue procesado por la "
                f"plataforma institucional de {institution.name}.",
                styles["receipt_footer"],
            )
        )

        return section

    def _format_file_size(self, size_bytes: int) -> str:
        size = float(size_bytes)

        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.2f} {unit}"
            size /= 1024

        return f"{size:.2f} GB"

    def _format_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"