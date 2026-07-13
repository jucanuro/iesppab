from __future__ import annotations

import hashlib
import logging
import secrets
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
from reportlab.platypus import (
    Image,
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
        certificate.issued_by = self.issued_by
        certificate.is_active = True
        certificate.revoked_at = None
        certificate.revoke_reason = ""
        certificate.save(
            update_fields=[
                "pdf_file",
                "issued_by",
                "is_active",
                "revoked_at",
                "revoke_reason",
                "updated_at",
            ]
        )

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

        code = self._generate_certificate_code()
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

    def _generate_certificate_code(self) -> str:
        year = timezone.now().year

        for _ in range(10):
            token = secrets.token_hex(4).upper()
            code = f"VQL-{year}-{token}"

            if not Certificate.objects.filter(code=code).exists():
                return code

        raise CertificateGenerationError(
            "No se pudo generar un código único de certificado."
        )

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

        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=1.7 * cm,
            leftMargin=1.7 * cm,
            topMargin=1.8 * cm,
            bottomMargin=1.5 * cm,
            title=f"Certificado {certificate.code}",
            author="Vertex Quant Labs",
        )

        story: list[Any] = []
        styles = self._build_styles()

        institution = report.document.institution
        owner = report.document.owner
        issued_by = certificate.issued_by

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
            "procesado por la plataforma institucional Vertex Quant Labs, "
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

        signature_name = (
            institution.certificate_signature_name
            or "Responsable de Integridad Académica"
        )
        signature_position = (
            institution.certificate_signature_position
            or "IESPP Alfonso Barrantes Lingán"
        )

        footer_table = Table(
            [
                [
                    Paragraph(
                        "<br/><br/>______________________________<br/>"
                        f"<b>{signature_name}</b><br/>"
                        f"{signature_position}<br/>"
                        f"Emitido por: {issued_by.get_full_name() or issued_by.username}",
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
                textColor=colors.HexColor("#7F1D1D"),
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

    def _decorate_page(self, canvas: Any, doc: Any) -> None:
        width, height = A4

        canvas.saveState()

        canvas.setFillColor(colors.HexColor("#7F1D1D"))
        canvas.rect(0, height - 1.0 * cm, width, 1.0 * cm, fill=1, stroke=0)

        canvas.setFillColor(colors.HexColor("#0F172A"))
        canvas.rect(0, 0, width, 0.45 * cm, fill=1, stroke=0)

        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(
            1.7 * cm,
            height - 0.65 * cm,
            "VERTEX QUANT LABS - INTEGRIDAD ACADEMICA",
        )

        canvas.setFont("Helvetica", 6.5)
        canvas.drawRightString(
            width - 1.7 * cm,
            0.17 * cm,
            f"Pagina {doc.page}",
        )

        canvas.restoreState()

    def _format_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"