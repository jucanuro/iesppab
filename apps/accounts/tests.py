from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.exceptions import InviteCodeError
from apps.accounts.models import (
    INVITE_CODE_ALPHABET,
    AdvisorInviteCode,
    AdvisorLink,
    StudentStatus,
    User,
    UserRole,
)
from apps.accounts.services import (
    AdvisorLinkService,
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

    def test_regenerar_desactiva_el_anterior_y_conserva_el_curso(self) -> None:
        service = InviteCodeService(self.advisor)
        first = service.create("Investigación II", "2026-II")
        second = service.regenerate(first.id)

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertNotEqual(first.code, second.code)
        self.assertEqual(second.course_name, "Investigación II")
        self.assertEqual(second.academic_period, "2026-II")

    def test_un_docente_tiene_varios_codigos_activos_uno_por_curso(self) -> None:
        service = InviteCodeService(self.advisor)
        service.create("Investigación II", "2026-II")
        service.create("Práctica Profesional", "2026-II")

        self.assertEqual(service.codes().filter(is_active=True).count(), 2)

    def test_no_repite_codigo_activo_del_mismo_curso(self) -> None:
        service = InviteCodeService(self.advisor)
        service.create("Investigación II", "2026-II")

        with self.assertRaises(InviteCodeError):
            service.create("investigación ii", "2026-II")

    def test_codigo_exige_curso(self) -> None:
        with self.assertRaises(InviteCodeError):
            InviteCodeService(self.advisor).create("", "")

    def test_activar_y_desactivar_un_codigo(self) -> None:
        service = InviteCodeService(self.advisor)
        code = service.create("Investigación II")

        service.set_active(code.id, False)
        code.refresh_from_db()
        self.assertFalse(code.is_active)

        service.set_active(code.id, True)
        code.refresh_from_db()
        self.assertTrue(code.is_active)

    def test_no_se_gestiona_el_codigo_de_otro_docente(self) -> None:
        code = InviteCodeService(self.advisor).create("Investigación II")

        with self.assertRaises(PermissionDenied):
            InviteCodeService(self.other_teacher).set_active(code.id, False)

    def test_alumno_no_genera_codigo(self) -> None:
        student = self.make_user("alumno", UserRole.STUDENT)
        self.client.force_login(student)

        response = self.client.post(
            reverse("accounts:invite-code"),
            {"action": "create", "course_name": "Curso"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AdvisorInviteCode.objects.exists())

    def test_docente_crea_y_desactiva_desde_la_pantalla(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(
            reverse("accounts:invite-code"),
            {"action": "create", "course_name": "Investigación II", "academic_period": "2026-II"},
        )
        code = AdvisorInviteCode.objects.get(owner=self.advisor, is_active=True)
        html = self.client.get(reverse("accounts:pending-students")).content.decode()
        self.assertIn(code.code, html)
        self.assertIn("Investigación II · 2026-II", html)

        self.client.post(
            reverse("accounts:invite-code"),
            {"action": "deactivate", "code_id": code.id},
        )
        self.assertFalse(AdvisorInviteCode.objects.filter(is_active=True).exists())

    def test_codigo_se_acepta_en_minusculas_y_con_espacios(self) -> None:
        code = InviteCodeService(self.advisor).regenerate().code

        invite = resolve_invite_code(f" {code[:4].lower()} {code[4:].lower()} ", self.institution.id)

        self.assertEqual(invite.owner, self.advisor)

    def test_codigo_de_otra_institucion_no_sirve(self) -> None:
        other = Institution.objects.create(name="Otro", slug="otro-cuentas")
        foreign = self.make_user("ajeno", UserRole.TEACHER, institution=other)
        code = InviteCodeService(foreign).regenerate().code

        with self.assertRaises(InviteCodeError):
            resolve_invite_code(code, self.institution.id)


class RegistrationWithCodeTests(AccountsTestBase):
    def test_registro_con_codigo_valido_vincula_y_puede_subir(self) -> None:
        code = InviteCodeService(self.advisor).create("Investigación II", "2026-II").code

        self.register(code)

        user = User.objects.get(email=f"ana@{DOMAIN}")
        link = AdvisorLink.objects.get(student=user)
        self.assertEqual(link.advisor, self.advisor)
        self.assertEqual(link.course_name, "Investigación II")
        self.assertEqual(link.academic_period, "2026-II")
        self.assertEqual(user.role, UserRole.STUDENT)
        self.assertEqual(user.student_status, StudentStatus.ENROLLED)
        self.assertTrue(user.is_enabled)

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


class MyStudentsTests(AccountsTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.mine = self.make_user("mio", UserRole.STUDENT)
        self.mine_alumni = self.make_user("mioex", UserRole.STUDENT, student_status=StudentStatus.ALUMNI)
        self.foreign = self.make_user("ajeno", UserRole.STUDENT)
        AdvisorLink.objects.create(student=self.mine, advisor=self.advisor, course_name="Investigación II")
        AdvisorLink.objects.create(student=self.mine_alumni, advisor=self.advisor, course_name="Tesis")
        AdvisorLink.objects.create(student=self.foreign, advisor=self.other_teacher, course_name="Práctica")
        self.orphan = self.make_user("huerfano", UserRole.STUDENT, is_enabled=False)
        self.orphan_mine = self.make_user("viejo", UserRole.STUDENT, is_enabled=False)
        AdvisorLink.objects.create(student=self.orphan_mine, advisor=self.advisor, course_name="")

    def test_asesor_solo_ve_sus_alumnos_vinculados(self) -> None:
        students = {link.student for link in StudentEnablementService(self.advisor).student_links()}

        self.assertEqual(students, {self.mine, self.mine_alumni, self.orphan_mine})

    def test_admin_ve_toda_la_institucion(self) -> None:
        students = {link.student for link in StudentEnablementService(self.admin).student_links()}

        self.assertEqual(students, {self.mine, self.mine_alumni, self.foreign, self.orphan_mine})

    def test_lista_distingue_alumno_exalumno_y_curso(self) -> None:
        self.client.force_login(self.advisor)

        html = self.client.get(reverse("accounts:pending-students")).content.decode()

        self.assertIn("Exalumno", html)
        self.assertIn("Validar como egresado", html)
        self.assertIn("Investigación II", html)
        self.assertNotIn(self.foreign.email, html)

    def test_validar_egresado_desde_mis_alumnos(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(
            reverse("documents:mark-alumni", args=[self.mine_alumni.id]),
            {"next": "my-students"},
        )

        self.mine_alumni.refresh_from_db()
        self.assertTrue(self.mine_alumni.is_validated_alumni)
        self.assertEqual(self.mine_alumni.alumni_validated_by, self.advisor)

    def test_docente_ajeno_no_valida_egresado(self) -> None:
        self.client.force_login(self.other_teacher)

        response = self.client.post(reverse("documents:mark-alumni", args=[self.mine_alumni.id]))

        self.assertEqual(response.status_code, 403)

    def test_pendientes_antiguos_por_asesor_y_admin(self) -> None:
        self.assertEqual(set(StudentEnablementService(self.advisor).pending_students()), {self.orphan_mine})
        self.assertEqual(
            set(StudentEnablementService(self.admin).pending_students()),
            {self.orphan, self.orphan_mine},
        )

    def test_asesor_no_habilita_alumnos_ajenos(self) -> None:
        self.client.force_login(self.advisor)

        self.client.post(reverse("accounts:bulk-enable-students"), {"student_ids": [self.orphan.id]})

        self.orphan.refresh_from_db()
        self.assertFalse(self.orphan.is_enabled)

    def test_admin_habilita_alumno_sin_asesor(self) -> None:
        StudentEnablementService(self.admin).enable([self.orphan.id])

        self.orphan.refresh_from_db()
        self.assertTrue(self.orphan.is_enabled)

    def test_alumno_no_entra_a_mis_alumnos(self) -> None:
        self.client.force_login(self.mine)

        self.assertEqual(self.client.get(reverse("accounts:pending-students")).status_code, 403)


class MyAdvisorsTests(AccountsTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("alumno", UserRole.STUDENT)
        self.code_a = InviteCodeService(self.advisor).create("Investigación II", "2026-II")
        self.code_b = InviteCodeService(self.other_teacher).create("Práctica Profesional", "2026-II")

    def add(self, code: str):
        self.client.force_login(self.student)
        return self.client.post(reverse("accounts:my-advisors"), {"invite_code": code})

    def test_alumno_con_varios_docentes(self) -> None:
        self.add(self.code_a.code)
        self.add(self.code_b.code)

        advisors = set(AdvisorLink.objects.filter(student=self.student).values_list("advisor", flat=True))
        self.assertEqual(advisors, {self.advisor.id, self.other_teacher.id})

    def test_mismo_codigo_dos_veces_no_duplica(self) -> None:
        self.add(self.code_a.code)
        response = self.add(self.code_a.code)

        self.assertEqual(AdvisorLink.objects.filter(student=self.student).count(), 1)
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any("Ya estás vinculado" in m for m in messages), messages)

    def test_codigo_regenerado_del_mismo_curso_no_duplica(self) -> None:
        self.add(self.code_a.code)
        new_code = InviteCodeService(self.advisor).regenerate(self.code_a.id)

        with self.assertRaises(InviteCodeError):
            AdvisorLinkService(self.student).add_by_code(new_code.code)

    def test_codigo_desactivado_muestra_error(self) -> None:
        InviteCodeService(self.advisor).set_active(self.code_a.id, False)

        response = self.add(self.code_a.code)

        self.assertFalse(AdvisorLink.objects.filter(student=self.student).exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn("desactivado", messages[0])

    def test_codigo_invalido_muestra_error(self) -> None:
        response = self.add("ZZZZZZZZ")

        self.assertFalse(AdvisorLink.objects.filter(student=self.student).exists())
        messages = [str(m) for m in response.wsgi_request._messages]
        self.assertIn("no existe", messages[0])

    def test_docente_no_entra_a_mis_docentes(self) -> None:
        self.client.force_login(self.advisor)

        self.assertEqual(self.client.get(reverse("accounts:my-advisors")).status_code, 403)

    def test_pantalla_lista_sus_vinculos(self) -> None:
        self.add(self.code_a.code)

        html = self.client.get(reverse("accounts:my-advisors")).content.decode()

        self.assertIn("Investigación II · 2026-II", html)


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


class AdvisorToLinkMigrationTests(TransactionTestCase):
    """0006 convierte el asesor único del usuario en un vínculo."""

    migrate_from = [("accounts", "0005_codigo_asesor"), ("core", "0001_initial")]
    migrate_to = [("accounts", "0006_vinculos_por_curso")]

    def test_asesor_existente_pasa_a_vinculo(self) -> None:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        Institution = old_apps.get_model("core", "Institution")
        OldUser = old_apps.get_model("accounts", "User")
        institution = Institution.objects.create(name="Migración", slug="migracion")
        teacher = OldUser.objects.create(
            username="docmig", email="docmig@example.com", role="TEACHER", institution=institution,
        )
        student = OldUser.objects.create(
            username="alumig", email="alumig@example.com", role="STUDENT",
            institution=institution, advisor=teacher,
        )
        OldUser.objects.create(
            username="sinasesor", email="sinasesor@example.com", role="STUDENT", institution=institution,
        )

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.migrate_to)
        new_apps = executor.loader.project_state(self.migrate_to).apps
        Link = new_apps.get_model("accounts", "AdvisorLink")

        self.assertEqual(Link.objects.count(), 1)
        link = Link.objects.get()
        self.assertEqual(link.student_id, student.id)
        self.assertEqual(link.advisor_id, teacher.id)

        # Deja la base en el estado más reciente para el resto de pruebas.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
