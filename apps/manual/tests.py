from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User, UserRole
from apps.core.models import Institution
from apps.manual import services as manual_services
from apps.manual.services import CAPTURAS, ManualService


class ManualTests(TestCase):
    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name='IESPP "Alfonso Barrantes Lingán"', is_active=True
        )

    def _user(self, role: str, **extra) -> User:
        return User.objects.create_user(
            username=f"user_{role.lower()}",
            email=f"{role.lower()}@example.com",
            password="clave-segura-123",
            role=role,
            institution=self.institution,
            **extra,
        )

    def _get(self, role: str, name: str = "manual:index"):
        self.client.force_login(self._user(role))
        return self.client.get(reverse(name))

    def test_requires_login(self) -> None:
        for name in ("manual:index", "manual:download"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302)
            self.assertIn("next=", response.url)

    def test_each_role_sees_only_its_sections(self) -> None:
        ids = {
            UserRole.STUDENT: "estudiante",
            UserRole.TEACHER: "docente",
            UserRole.DIRECTOR: "director",
        }
        for role, key in ids.items():
            with self.subTest(role=role):
                self.client.logout()
                User.objects.filter(username=f"user_{role.lower()}").delete()
                response = self._get(role)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'id="{key}"')
                for other in set(ids.values()) - {key}:
                    self.assertNotContains(response, f'id="{other}"')
                self.assertNotContains(response, 'id="administrador"')

    def test_admin_sees_all_sections(self) -> None:
        response = self._get(UserRole.ADMIN)
        for key in ("estudiante", "docente", "director", "administrador"):
            self.assertContains(response, f'id="{key}"')

    def test_download_returns_pdf(self) -> None:
        response = self._get(UserRole.STUDENT, "manual:download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertGreater(len(response.content), 1000)

    def test_menu_link_visible(self) -> None:
        response = self._get(UserRole.STUDENT, "documents:upload")
        self.assertContains(response, reverse("manual:index"))
        self.assertContains(response, "Manual de usuario")


class ManualPdfTests(TestCase):
    """PDF por rol, capturas ausentes y exactitud de la valoración global."""

    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name='IESPP "Alfonso Barrantes Lingán"', is_active=True
        )

    def _user(self, role: str) -> User:
        return User.objects.create_user(
            username=f"pdf_{role.lower()}",
            email=f"pdf_{role.lower()}@example.com",
            password="clave-segura-123",
            role=role,
            institution=self.institution,
        )

    def test_pdf_is_generated_for_every_role(self) -> None:
        for role in (UserRole.STUDENT, UserRole.TEACHER, UserRole.DIRECTOR, UserRole.ADMIN):
            with self.subTest(role=role):
                pdf = ManualService().render_pdf(self._user(role))
                self.assertTrue(pdf.startswith(b"%PDF"))
                # Las capturas existentes se incrustan como imágenes.
                self.assertIn(b"/Subtype /Image", pdf)

    def test_missing_screenshots_do_not_break_pdf_or_page(self) -> None:
        with mock.patch.object(manual_services.finders, "find", return_value=None):
            for role in (UserRole.STUDENT, UserRole.TEACHER, UserRole.DIRECTOR, UserRole.ADMIN):
                with self.subTest(role=role):
                    user = self._user(role)
                    pdf = ManualService().render_pdf(user)
                    self.assertTrue(pdf.startswith(b"%PDF"))
                    self.client.force_login(user)
                    response = self.client.get(reverse("manual:index"))
                    self.assertContains(response, "Captura no disponible")
                    self.client.logout()
                    user.delete()

    def test_pdf_shots_use_absolute_file_paths(self) -> None:
        shots = ManualService().build_shots(for_pdf=True)
        self.assertEqual(set(shots), set(CAPTURAS))
        for key, shot in shots.items():
            with self.subTest(shot=key):
                self.assertTrue(shot["available"])
                self.assertTrue(shot["src"].startswith("file:///"))

    def test_screenshots_per_role(self) -> None:
        esperado = {
            UserRole.STUDENT: {"01-login", "02-bandeja", "03-subir"},
            UserRole.TEACHER: {"01-login", "02-bandeja", "03-subir", "04-acciones", "05-reporte-resumen"},
            UserRole.DIRECTOR: {"01-login", "04-acciones", "05-reporte-resumen"},
        }
        for role, nombres in esperado.items():
            with self.subTest(role=role):
                self.client.force_login(self._user(role))
                html = self.client.get(reverse("manual:index")).content.decode()
                for nombre in nombres:
                    self.assertIn(nombre, html)
                self.assertNotIn("08-admin", html)
                self.client.logout()
                User.objects.filter(username=f"pdf_{role.lower()}").delete()
        self.client.force_login(self._user(UserRole.ADMIN))
        self.assertContains(self.client.get(reverse("manual:index")), "08-admin")

    def test_manual_explains_global_assessment_accurately(self) -> None:
        self.client.force_login(self._user(UserRole.TEACHER))
        response = self.client.get(reverse("manual:index"))
        self.assertContains(response, "Valoración global")
        self.assertContains(response, "no bloquea el certificado")
        self.assertContains(response, "peor")
        self.assertContains(response, "Glosario")
        self.assertContains(response, "Banda de riesgo")
        self.assertContains(response, "class=\"m-diagram\"", count=3)
