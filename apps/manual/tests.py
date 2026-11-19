from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User, UserRole
from apps.core.models import Institution


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
