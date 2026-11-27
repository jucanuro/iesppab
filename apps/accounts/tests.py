from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.accounts.exceptions import InviteCodeError
from apps.accounts.models import (
    INVITE_CODE_ALPHABET,
    AdvisorInviteCode,
    StudentStatus,
    User,
    UserRole,
)
from apps.accounts.services import (
    InviteCodeService,
    StudentEnablementService,
    resolve_invite_code,
)
from apps.core.models import Institution

DOMAIN = "example.edu.pe"


class AccountsTestBase(TestCase):
    def setUp(self) -> None:
        self.institution = Institution.objects.create(
            name="Instituto",
            slug="instituto-cuentas",
            email_domain=DOMAIN,
            is_active=True,
        )
        self.advisor = self.make_user("asesor", UserRole.TEACHER)
        self.other_teacher = self.make_user("docente2", UserRole.TEACHER)
        self.admin = self.make_user("admin", UserRole.ADMIN)

    def make_user(self, name: str, role: str, **extra) -> User:
        return User.objects.create_user(
            username=name,
            email=f"{name}@{DOMAIN}",
            password="clave-segura-123",
            role=role,
            institution=extra.pop("institution", self.institution),
            **extra,
        )

    def register(self, code: str, status: str = StudentStatus.ENROLLED, email: str = "ana"):
        return self.client.post(
            reverse("accounts:register"),
            {
                "first_name": "Ana",
                "last_name": "Rojas",
                "email": f"{email}@{DOMAIN}",
                "password1": "ClaveSegura#2026",
                "password2": "ClaveSegura#2026",
                "student_status": status,
                "invite_code": code,
            },
        )


class InviteCodeTests(AccountsTestBase):
    def test_genera_codigo_de_ocho_caracteres_sin_ambiguos(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        self.assertEqual(len(code), 8)
        self.assertTrue(all(char in INVITE_CODE_ALPHABET for char in code))
        for ambiguous in "0O1I":
            self.assertNotIn(ambiguous, INVITE_CODE_ALPHABET)

    def test_regenerar_desactiva_el_anterior(self) -> None:
        service = InviteCodeService(self.advisor)
        first = service.regenerate()
        second = service.regenerate()

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertNotEqual(first.code, second.code)
        self.assertEqual(service.active_code(), second)

    def test_alumno_no_genera_codigo(self) -> None:
        student = self.make_user("alumno", UserRole.STUDENT)
        self.client.force_login(student)

        response = self.client.post(reverse("accounts:invite-code"), {"action": "generate"})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AdvisorInviteCode.objects.exists())

    def test_docente_genera_y_desactiva_desde_la_pantalla(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(reverse("accounts:invite-code"), {"action": "generate"})
        code = AdvisorInviteCode.objects.get(owner=self.advisor, is_active=True)
        html = self.client.get(reverse("accounts:pending-students")).content.decode()
        self.assertIn(code.code, html)

        self.client.post(reverse("accounts:invite-code"), {"action": "deactivate"})
        self.assertFalse(AdvisorInviteCode.objects.filter(is_active=True).exists())

    def test_codigo_se_acepta_en_minusculas_y_con_espacios(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        advisor = resolve_invite_code(f" {code[:4].lower()} {code[4:].lower()} ", self.institution.id)

        self.assertEqual(advisor, self.advisor)

    def test_codigo_de_otra_institucion_no_sirve(self) -> None:
        other = Institution.objects.create(name="Otro", slug="otro-cuentas")
        foreign = self.make_user("ajeno", UserRole.TEACHER, institution=other)
        code = InviteCodeService(foreign).regenerate().code

        with self.assertRaises(InviteCodeError):
            resolve_invite_code(code, self.institution.id)


class RegistrationWithCodeTests(AccountsTestBase):
    def test_registro_con_codigo_valido_relaciona_al_asesor(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        self.register(code)

        user = User.objects.get(email=f"ana@{DOMAIN}")
        self.assertEqual(user.advisor, self.advisor)
        self.assertEqual(user.role, UserRole.STUDENT)
        self.assertEqual(user.student_status, StudentStatus.ENROLLED)
        self.assertFalse(user.is_enabled)

    def test_registro_como_exalumno(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        self.register(code, status=StudentStatus.ALUMNI)

        user = User.objects.get(email=f"ana@{DOMAIN}")
        self.assertEqual(user.student_status, StudentStatus.ALUMNI)
        self.assertIsNone(user.alumni_validated_at)

    def test_registro_con_codigo_inexistente_falla_con_mensaje(self) -> None:
        response = self.register("ABCDEFGH")

        self.assertFalse(User.objects.filter(email=f"ana@{DOMAIN}").exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn("El código no existe", messages[0])

    def test_registro_con_codigo_desactivado_falla(self) -> None:
        service = InviteCodeService(self.advisor)
        code = service.regenerate().code
        service.deactivate()

        response = self.register(code)

        self.assertFalse(User.objects.filter(email=f"ana@{DOMAIN}").exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn("desactivado", messages[0])

    def test_registro_sin_codigo_falla(self) -> None:
        self.register("")

        self.assertFalse(User.objects.filter(email=f"ana@{DOMAIN}").exists())

    def test_registro_exige_condicion(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        self.register(code, status="")

        self.assertFalse(User.objects.filter(email=f"ana@{DOMAIN}").exists())

    def test_formulario_muestra_opciones_y_codigo(self) -> None:
        html = self.client.get(reverse("accounts:register")).content.decode()

        self.assertIn("Soy alumno", html)
        self.assertIn("Soy exalumno", html)
        self.assertIn('name="invite_code"', html)


class PendingStudentsTests(AccountsTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.mine = self.make_user("mio", UserRole.STUDENT, advisor=self.advisor, is_enabled=False)
        self.mine_alumni = self.make_user(
            "mioex",
            UserRole.STUDENT,
            advisor=self.advisor,
            is_enabled=False,
            student_status=StudentStatus.ALUMNI,
        )
        self.foreign = self.make_user("ajeno", UserRole.STUDENT, advisor=self.other_teacher, is_enabled=False)
        self.orphan = self.make_user("huerfano", UserRole.STUDENT, is_enabled=False)

    def test_asesor_solo_ve_los_de_su_codigo(self) -> None:
        pending = set(StudentEnablementService(self.advisor).pending_students())

        self.assertEqual(pending, {self.mine, self.mine_alumni})

    def test_admin_ve_todos_incluidos_sin_asesor(self) -> None:
        pending = set(StudentEnablementService(self.admin).pending_students())

        self.assertEqual(pending, {self.mine, self.mine_alumni, self.foreign, self.orphan})

    def test_lista_distingue_alumno_y_exalumno(self) -> None:
        self.client.force_login(self.advisor)

        html = self.client.get(reverse("accounts:pending-students")).content.decode()

        self.assertIn("Exalumno", html)
        self.assertIn("Validar como egresado y habilitar", html)
        self.assertNotIn(self.foreign.email, html)

    def test_habilitar_alumno(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(reverse("accounts:bulk-enable-students"), {"student_ids": [self.mine.id]})

        self.mine.refresh_from_db()
        self.assertTrue(self.mine.is_enabled)

    def test_validar_exalumno_y_habilitar(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(
            reverse("accounts:bulk-enable-students"),
            {"student_ids": [self.mine_alumni.id], "validate_alumni": "1"},
        )

        self.mine_alumni.refresh_from_db()
        self.assertTrue(self.mine_alumni.is_enabled)
        self.assertTrue(self.mine_alumni.is_validated_alumni)
        self.assertEqual(self.mine_alumni.alumni_validated_by, self.advisor)

    def test_asesor_no_habilita_alumnos_ajenos(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(
            reverse("accounts:bulk-enable-students"),
            {"student_ids": [self.foreign.id, self.orphan.id]},
        )

        self.foreign.refresh_from_db()
        self.orphan.refresh_from_db()
        self.assertFalse(self.foreign.is_enabled)
        self.assertFalse(self.orphan.is_enabled)

    def test_admin_habilita_alumno_sin_asesor(self) -> None:
        StudentEnablementService(self.admin).enable([self.orphan.id])

        self.orphan.refresh_from_db()
        self.assertTrue(self.orphan.is_enabled)

    def test_alumno_no_entra_a_pendientes(self) -> None:
        self.client.force_login(self.mine)

        self.assertEqual(self.client.get(reverse("accounts:pending-students")).status_code, 403)
        response = self.client.post(
            reverse("accounts:bulk-enable-students"),
            {"student_ids": [self.mine.id]},
        )
        self.assertEqual(response.status_code, 403)


class AdminLoginTests(AccountsTestBase):
    """/admin/login/ acepta usuario o correo; sigue exigiendo is_staff."""

    def setUp(self) -> None:
        super().setUp()
        self.staff = self.make_user("jefe", UserRole.ADMIN, is_staff=True)
        self.no_staff_admin = self.make_user("adminsinstaff", UserRole.ADMIN)

    def admin_login(self, username: str, password: str = "clave-segura-123"):
        return self.client.post(
            reverse("admin:login"),
            {"username": username, "password": password, "next": "/admin/"},
        )

    def test_entra_con_usuario(self) -> None:
        response = self.admin_login("jefe")

        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_entra_con_correo_institucional(self) -> None:
        response = self.admin_login(f"JEFE@{DOMAIN.upper()}")

        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_correo_con_clave_incorrecta_no_entra(self) -> None:
        response = self.admin_login(f"jefe@{DOMAIN}", "otra-clave")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_usuario_con_clave_incorrecta_no_entra(self) -> None:
        response = self.admin_login("jefe", "otra-clave")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_sin_is_staff_no_entra_al_panel(self) -> None:
        for username in ("adminsinstaff", f"adminsinstaff@{DOMAIN}"):
            response = self.admin_login(username)

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_correo_no_sirve_como_credencial_para_no_staff(self) -> None:
        from django.contrib.auth import authenticate

        self.assertIsNone(authenticate(username=f"asesor@{DOMAIN}", password="clave-segura-123"))
        self.assertIsNotNone(authenticate(username="asesor", password="clave-segura-123"))

    def test_login_institucional_sigue_exigiendo_correo_a_no_privilegiados(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "asesor", "password": "clave-segura-123"},
        )

        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
