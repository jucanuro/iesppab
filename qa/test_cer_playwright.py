"""QA de extremo a extremo de ORI-CER con Playwright (T13).

Solo local: usa la base de pruebas de Django (SQLite) y datos sembrados aquí;
no toca producción ni imprime secretos. Se ejecuta con
`python manage.py test qa` (o dentro de la suite completa). Si Chromium no
está disponible, las pruebas se omiten.
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path

# Playwright (API síncrona) corre un bucle de eventos en el hilo de la prueba.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")
os.environ.setdefault("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "ubuntu22.04-x64")

from django.core.files.base import ContentFile
from django.template.defaultfilters import floatformat
from django.test import Client, LiveServerTestCase
from django.urls import reverse

from apps.accounts.models import User, UserRole
from apps.certificates.models import Certificate
from apps.core.models import Institution
from apps.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentText,
)
from apps.reports.ai_estimate import build_ai_estimate
from apps.reports.models import (
    AnalysisReport,
    FindingType,
    ReportRiskLevel,
    ReportSource,
    SourceType,
)

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

TEXT = (
    "El presente trabajo analiza la metodologia aplicada en el estudio de "
    "los factores institucionales relacionados con el aprendizaje. Este "
    "parrafo contiene un fragmento redactado por un modelo de lenguaje."
)
SCREENSHOTS = Path(tempfile.gettempdir()) / "cer-qa"


class CerPlaywrightTests(LiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.playwright = None
        cls.browser = None

        if sync_playwright is None:
            return

        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch()
        except Exception:  # navegador no instalado
            if cls.playwright:
                cls.playwright.stop()
            cls.playwright = None

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.browser:
            cls.browser.close()
        if cls.playwright:
            cls.playwright.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        if self.browser is None:
            self.skipTest("Playwright/Chromium no disponible.")

        SCREENSHOTS.mkdir(exist_ok=True)
        self.institution = Institution.objects.create(
            name="Instituto QA", slug="instituto-qa"
        )
        self.student = self._user("qa-alumno")
        self.stranger = self._user("qa-ajeno")
        self.document = Document(
            institution=self.institution,
            owner=self.student,
            uploaded_by=self.student,
            title="Tesis QA",
            kind=DocumentKind.OTHER,
            original_filename="qa.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(TEXT),
            sha256_hash="hash-qa",
            status=DocumentStatus.COMPLETED,
        )
        self.document.original_file.save(
            "qa.pdf", ContentFile(TEXT.encode()), save=False
        )
        self.document.save()
        DocumentText.objects.create(
            document=self.document,
            content=TEXT,
            word_count=len(TEXT.split()),
            character_count=len(TEXT),
        )
        self.report = AnalysisReport.objects.create(
            document=self.document,
            similarity_percent=Decimal("12.00"),
            web_similarity_percent=Decimal("12.00"),
            internal_similarity_percent=Decimal("0.00"),
            ai_probability_percent=Decimal("35.00"),
            risk_level=ReportRiskLevel.LOW,
            engine_version="motor-qa-1.0",
            summary={"ai_score_breakdown": {"classifier": {"ai_percent": "35.00"}}},
        )
        source = ReportSource.objects.create(
            report=self.report,
            source_type=SourceType.WEB,
            title="Fuente web QA",
            url="https://example.com/qa",
            domain="example.com",
            matched_percent=Decimal("12.00"),
        )
        start = TEXT.index("metodologia")
        self.report.findings.create(
            source=source,
            finding_type=FindingType.SIMILARITY,
            start_offset=start,
            end_offset=start + len("metodologia aplicada"),
            text_excerpt="metodologia aplicada",
            confidence_percent=Decimal("12.00"),
        )
        self.certificate = Certificate.objects.create(
            report=self.report,
            code="CER-QA-0001",
            verification_hash="d" * 64,
            issued_by=self.student,
        )

    def _user(self, username: str) -> User:
        return User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="clave-segura-123",
            role=UserRole.STUDENT,
            institution=self.institution,
        )

    def _context(self, user: User | None = None):
        context = self.browser.new_context()
        if user is not None:
            client = Client()
            client.force_login(user)
            cookie = client.cookies["sessionid"].value
            context.add_cookies(
                [{"name": "sessionid", "value": cookie, "url": self.live_server_url}]
            )
        return context

    def _text(self, page, path: str) -> str:
        response = page.goto(f"{self.live_server_url}{path}")
        self.assertLess(response.status, 400)
        # text_content incluye las notas ocultas (popovers) del reporte.
        return " ".join((page.text_content("body") or "").split())

    def test_verify_matches_report_detail(self) -> None:
        estimate = build_ai_estimate(self.report)
        context = self._context(self.student)
        page = context.new_page()

        verify = self._text(
            page, reverse("certificates:verify", args=[self.certificate.verification_hash])
        )
        page.screenshot(path=str(SCREENSHOTS / "verify.png"), full_page=True)
        detail = self._text(
            page, reverse("reports:detail", kwargs={"pk": self.document.id})
        )
        page.screenshot(path=str(SCREENSHOTS / "detail.png"), full_page=True)
        context.close()

        for value in (
            f"{floatformat(estimate.percent, 1)}%",
            estimate.band_label,
            estimate.note,
        ):
            with self.subTest(value=value):
                self.assertIn(" ".join(value.split()), verify)
                self.assertIn(" ".join(value.split()), detail)

        self.assertIn(estimate.version_label, verify)
        self.assertIn(estimate.version_label, detail)
        from django.utils import timezone

        self.assertIn(
            timezone.localtime(estimate.generated_at).strftime("%d/%m/%Y"), detail
        )  # hora de America/Lima, como la página
        self.assertIn("CER-QA-0001", verify)

    def test_invalid_hash_says_not_valid_without_500(self) -> None:
        page = self._context().new_page()

        response = page.goto(
            f"{self.live_server_url}"
            + reverse("certificates:verify", args=["e" * 64])
        )

        self.assertEqual(response.status, 200)
        self.assertIn("no válido", " ".join(page.inner_text("body").split()))
        page.screenshot(path=str(SCREENSHOTS / "invalid.png"))

    def test_both_pdfs_download_and_stranger_gets_403(self) -> None:
        for name in ("reports:download_highlighted", "reports:download_ai_report"):
            url = f"{self.live_server_url}" + reverse(name, kwargs={"pk": self.document.id})
            with self.subTest(url=name):
                owner = self._context(self.student)
                response = owner.request.get(url)
                self.assertEqual(response.status, 200)
                self.assertIn("application/pdf", response.headers["content-type"])
                self.assertTrue(response.body().startswith(b"%PDF"))
                owner.close()

                other = self._context(self.stranger)
                self.assertEqual(other.request.get(url).status, 403)
                other.close()

    def test_verify_prints_on_a4_without_overflow(self) -> None:
        page = self._context().new_page()
        page.goto(
            f"{self.live_server_url}"
            + reverse("certificates:verify", args=[self.certificate.verification_hash])
        )
        out = SCREENSHOTS / "verify.pdf"

        pdf = page.pdf(path=str(out), format="A4")

        self.assertTrue(pdf.startswith(b"%PDF"))
        page.emulate_media(media="print")
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        self.assertLessEqual(overflow, 0)
