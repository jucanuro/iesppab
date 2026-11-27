from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import StudentStatus, User, UserRole
from apps.core.models import Institution
from apps.documents.exceptions import (
    AuthorizationError,
    DocumentUploadError,
    SubmissionLimitError,
)
from apps.documents.models import (
    AuthorizationStatus,
    Document,
    DocumentStatus,
    PermissionProfile,
    RolePermission,
)
from apps.documents.services import (
    DocumentUploadDTO,
    DocumentUploadService,
    SubmissionAuthorizationService,
    SubmissionPolicy,
)

DELAY = "apps.analysis.tasks.run_document_analysis.delay"


class AuthorizationTestBase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._media_root = tempfile.mkdtemp()
        self._media_override = override_settings(MEDIA_ROOT=self._media_root)
        self._media_override.enable()

        self.institution = Institution.objects.create(
            name="Instituto de prueba",
            slug="instituto-autorizacion",
        )
        self.other_institution = Institution.objects.create(
            name="Otro instituto",
            slug="otro-instituto-autorizacion",
        )
        self.advisor = self.make_user("asesor", UserRole.TEACHER)
        self.other_teacher = self.make_user("docente2", UserRole.TEACHER)
        self.director = self.make_user("director", UserRole.DIRECTOR)
        self.admin = self.make_user("admin", UserRole.ADMIN)
        self.student = self.make_user("alumno", UserRole.STUDENT)
        self.alumni = self.make_user(
            "exalumno",
            UserRole.STUDENT,
            student_status=StudentStatus.ALUMNI,
        )
        self._counter = 0

    def tearDown(self) -> None:
        self._media_override.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().tearDown()

    def make_user(self, name: str, role: str, **extra) -> User:
        return User.objects.create_user(
            username=name,
            email=f"{name}@example.com",
            password="clave-segura-123",
            role=role,
            institution=extra.pop("institution", self.institution),
            **extra,
        )

    def upload(self, user: User, advisor: User | None = None, owner: User | None = None) -> Document:
        self._counter += 1
        payload = (
            b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n"
            + f"% documento {self._counter}\n".encode()
            + b"trailer << /Root 1 0 R >>\n%%EOF\n"
        )
        if owner is None and not user.is_student_role:
            owner = user
        dto = DocumentUploadDTO(
            title=f"Documento {self._counter}",
            kind="THESIS",
            course_name="",
            academic_period="",
            owner_id=owner.id if owner else None,
            advisor_id=advisor.id if advisor else None,
            uploaded_file=SimpleUploadedFile("tesis.pdf", payload, content_type="application/pdf"),
        )
        return DocumentUploadService(uploaded_by=user).execute(dto)


class UploadRulesTests(AuthorizationTestBase):
    def test_alumno_sin_asesor_no_puede_subir(self) -> None:
        with self.assertRaisesMessage(DocumentUploadError, "Elige a tu asesor"):
            self.upload(self.student)

    def test_documento_de_alumno_queda_pendiente_y_no_se_analiza(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)

        self.assertEqual(document.authorization_status, AuthorizationStatus.PENDING)
        self.assertEqual(document.status, DocumentStatus.UPLOADED)

        self.client.force_login(self.student)
        with mock.patch(DELAY) as delay:
            self.client.post(reverse("analysis:analyze", args=[document.id]))

        delay.assert_not_called()
        document.refresh_from_db()
        self.assertEqual(document.status, DocumentStatus.UPLOADED)

    def test_alumno_en_formacion_no_tiene_tope(self) -> None:
        for _ in range(6):
            self.upload(self.student, advisor=self.advisor)

        self.assertIsNone(SubmissionPolicy.quota(self.student).limit)

    def test_docente_maximo_cinco_envios(self) -> None:
        for _ in range(5):
            document = self.upload(self.advisor)
            self.assertEqual(document.authorization_status, AuthorizationStatus.NOT_REQUIRED)

        with self.assertRaisesMessage(SubmissionLimitError, "máximo de 5 envíos"):
            self.upload(self.advisor)

    def test_envios_adicionales_amplian_el_tope(self) -> None:
        self.advisor.extra_submissions = 1
        self.advisor.save()

        for _ in range(6):
            self.upload(self.advisor)

        with self.assertRaises(SubmissionLimitError):
            self.upload(self.advisor)

    def test_envio_fallido_no_consume_tope(self) -> None:
        for _ in range(5):
            self.upload(self.advisor)
        Document.objects.filter(owner=self.advisor).update(status=DocumentStatus.FAILED)

        self.upload(self.advisor)

    def test_admin_y_director_sin_limite(self) -> None:
        for user in (self.admin, self.director):
            for _ in range(7):
                document = self.upload(user)
                self.assertEqual(document.authorization_status, AuthorizationStatus.NOT_REQUIRED)

    def test_docente_que_sube_por_un_alumno_no_consume_su_tope(self) -> None:
        for _ in range(6):
            document = self.upload(self.advisor, owner=self.student)
            self.assertEqual(document.authorization_status, AuthorizationStatus.NOT_REQUIRED)

        self.assertEqual(SubmissionPolicy.quota(self.advisor).used, 0)

    def test_exalumno_sin_validar_requiere_autorizacion_y_tope_tres(self) -> None:
        for _ in range(3):
            document = self.upload(self.alumni, advisor=self.advisor)
            self.assertEqual(document.authorization_status, AuthorizationStatus.PENDING)

        with self.assertRaisesMessage(SubmissionLimitError, "máximo de 3 envíos"):
            self.upload(self.alumni, advisor=self.advisor)

    def test_rechazado_no_consume_tope_del_exalumno(self) -> None:
        documents = [self.upload(self.alumni, advisor=self.advisor) for _ in range(3)]
        SubmissionAuthorizationService(self.advisor).reject(documents[0].id, "Incompleto")

        self.upload(self.alumni, advisor=self.advisor)

    def test_exalumno_validado_no_requiere_autorizacion(self) -> None:
        SubmissionAuthorizationService(self.admin).mark_alumni(self.alumni.id)
        self.alumni.refresh_from_db()

        document = self.upload(self.alumni)

        self.assertEqual(document.authorization_status, AuthorizationStatus.NOT_REQUIRED)


class AuthorizationDecisionTests(AuthorizationTestBase):
    def test_aprobar_encola_el_analisis(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)

        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            SubmissionAuthorizationService(self.advisor).approve(document.id)

        document.refresh_from_db()
        self.assertEqual(document.authorization_status, AuthorizationStatus.APPROVED)
        self.assertEqual(document.authorized_by, self.advisor)
        self.assertIsNotNone(document.authorized_at)
        self.assertEqual(document.status, DocumentStatus.QUEUED)
        delay.assert_called_once()

    def test_aprobado_se_puede_reanalizar(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)
        with mock.patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            SubmissionAuthorizationService(self.advisor).approve(document.id)
        Document.objects.filter(pk=document.pk).update(status=DocumentStatus.COMPLETED)

        self.client.force_login(self.student)
        with mock.patch(DELAY) as delay:
            self.client.post(reverse("analysis:analyze", args=[document.id]))

        delay.assert_called_once()

    def test_rechazar_guarda_motivo_y_bloquea_analisis(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)

        SubmissionAuthorizationService(self.advisor).reject(document.id, "Falta resultados")

        document.refresh_from_db()
        self.assertEqual(document.authorization_status, AuthorizationStatus.REJECTED)
        self.assertEqual(document.authorization_note, "Falta resultados")
        self.client.force_login(self.student)
        with mock.patch(DELAY) as delay:
            self.client.post(reverse("analysis:analyze", args=[document.id]))
        delay.assert_not_called()

    def test_solo_el_asesor_elegido_o_admin_decide(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)

        for intruder in (self.other_teacher, self.director):
            with self.assertRaises(PermissionDenied):
                SubmissionAuthorizationService(intruder).approve(document.id)

        foreign_admin = self.make_user(
            "admin2", UserRole.ADMIN, institution=self.other_institution,
        )
        with self.assertRaises(PermissionDenied):
            SubmissionAuthorizationService(foreign_admin).reject(document.id)

        with mock.patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            SubmissionAuthorizationService(self.admin).approve(document.id)

    def test_solicitud_resuelta_no_se_decide_dos_veces(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)
        SubmissionAuthorizationService(self.advisor).reject(document.id)

        with self.assertRaises(AuthorizationError):
            SubmissionAuthorizationService(self.advisor).approve(document.id)

    def test_primer_envio_de_exalumno_valida_y_autoriza(self) -> None:
        document = self.upload(self.alumni, advisor=self.advisor)

        with mock.patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            SubmissionAuthorizationService(self.advisor).approve(
                document.id, validate_alumni=True,
            )

        self.alumni.refresh_from_db()
        self.assertTrue(self.alumni.is_validated_alumni)
        self.assertEqual(self.alumni.alumni_validated_by, self.advisor)
        self.assertFalse(SubmissionPolicy.requires_authorization(self.alumni))

    def test_validar_egresado_solo_para_exalumnos(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)

        with self.assertRaises(AuthorizationError):
            SubmissionAuthorizationService(self.advisor).approve(
                document.id, validate_alumni=True,
            )

    def test_asesor_marca_egresado_a_su_alumno(self) -> None:
        self.upload(self.student, advisor=self.advisor)

        SubmissionAuthorizationService(self.advisor).mark_alumni(self.student.id)

        self.student.refresh_from_db()
        self.assertEqual(self.student.student_status, StudentStatus.ALUMNI)
        self.assertTrue(self.student.is_validated_alumni)

    def test_docente_ajeno_no_marca_egresado(self) -> None:
        self.upload(self.student, advisor=self.advisor)

        with self.assertRaises(PermissionDenied):
            SubmissionAuthorizationService(self.other_teacher).mark_alumni(self.student.id)


class AuthorizationViewsTests(AuthorizationTestBase):
    def test_alumno_no_ve_la_bandeja_ni_decide(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)
        self.client.force_login(self.student)

        self.assertEqual(self.client.get(reverse("documents:authorizations")).status_code, 403)
        response = self.client.post(
            reverse("documents:authorization-decision", args=[document.id]),
            {"decision": "approve"},
        )
        self.assertEqual(response.status_code, 403)
        document.refresh_from_db()
        self.assertEqual(document.authorization_status, AuthorizationStatus.PENDING)

    def test_asesor_solo_ve_sus_solicitudes(self) -> None:
        mine = self.upload(self.student, advisor=self.advisor)
        other = self.upload(self.student, advisor=self.other_teacher)
        self.client.force_login(self.advisor)

        html = self.client.get(reverse("documents:authorizations")).content.decode()

        self.assertIn(mine.title, html)
        self.assertNotIn(other.title, html)

    def test_bandeja_muestra_mensaje_de_validacion_de_exalumno(self) -> None:
        self.upload(self.alumni, advisor=self.advisor)
        self.client.force_login(self.advisor)

        html = self.client.get(reverse("documents:authorizations")).content.decode()

        self.assertIn("Primer envío de un exalumno: valida que es egresado.", html)
        self.assertIn("Validar como egresado y autorizar", html)

    def test_post_de_aprobacion_encola(self) -> None:
        document = self.upload(self.student, advisor=self.advisor)
        self.client.force_login(self.advisor)

        with mock.patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("documents:authorization-decision", args=[document.id]),
                {"decision": "approve"},
            )

        delay.assert_called_once()

    def test_bandeja_del_alumno_muestra_pendiente(self) -> None:
        self.upload(self.student, advisor=self.advisor)
        self.client.force_login(self.student)

        html = self.client.get(reverse("documents:upload")).content.decode()

        self.assertIn("Pendiente de autorización", html)
        self.assertIn("Esperando autorización del asesor", html)


class RolePermissionPolicyTests(AuthorizationTestBase):
    """La política se lee de «Permisos por rol» y cambia el comportamiento."""

    def test_admin_cambia_el_tope_del_docente(self) -> None:
        RolePermission.objects.filter(profile=PermissionProfile.TEACHER).update(
            max_submissions=1,
        )

        self.upload(self.advisor)
        with self.assertRaises(SubmissionLimitError):
            self.upload(self.advisor)

    def test_docente_sin_tope_si_se_vacia_el_maximo(self) -> None:
        RolePermission.objects.filter(profile=PermissionProfile.TEACHER).update(
            max_submissions=None,
        )

        for _ in range(7):
            self.upload(self.advisor)

    def test_alumno_sin_autorizacion_si_el_admin_la_quita(self) -> None:
        RolePermission.objects.filter(profile=PermissionProfile.STUDENT).update(
            requires_authorization=False,
        )

        document = self.upload(self.student)

        self.assertEqual(document.authorization_status, AuthorizationStatus.NOT_REQUIRED)

    def test_exalumno_validado_agota_sus_libres_y_vuelve_a_pedir_autorizacion(self) -> None:
        RolePermission.objects.filter(profile=PermissionProfile.ALUMNI).update(
            free_submissions=1,
            max_submissions=None,
        )
        SubmissionAuthorizationService(self.admin).mark_alumni(self.alumni.id)
        self.alumni.refresh_from_db()

        free = self.upload(self.alumni, advisor=self.advisor)
        self.assertEqual(free.authorization_status, AuthorizationStatus.NOT_REQUIRED)

        second = self.upload(self.alumni, advisor=self.advisor)
        self.assertEqual(second.authorization_status, AuthorizationStatus.PENDING)

    def test_politica_sembrada_por_la_migracion(self) -> None:
        rules = {rule.profile: rule for rule in RolePermission.objects.all()}

        self.assertEqual(set(rules), set(PermissionProfile.values))
        self.assertTrue(rules["STUDENT"].requires_authorization)
        self.assertIsNone(rules["STUDENT"].max_submissions)
        self.assertTrue(rules["ALUMNI"].requires_first_validation)
        self.assertEqual(rules["ALUMNI"].free_submissions, 3)
        self.assertEqual(rules["ALUMNI"].max_submissions, 3)
        self.assertEqual(rules["TEACHER"].max_submissions, 5)
        self.assertIsNone(rules["DIRECTOR"].max_submissions)
        self.assertIsNone(rules["ADMIN"].max_submissions)


class FixedAdvisorTests(AuthorizationTestBase):
    def test_alumno_con_asesor_por_codigo_envia_siempre_a_su_asesor(self) -> None:
        self.student.advisor = self.advisor
        self.student.save()

        document = self.upload(self.student, advisor=self.other_teacher)

        self.assertEqual(document.advisor, self.advisor)

    def test_alumno_con_asesor_no_necesita_elegirlo(self) -> None:
        self.student.advisor = self.advisor
        self.student.save()

        document = self.upload(self.student)

        self.assertEqual(document.advisor, self.advisor)
        self.assertEqual(document.authorization_status, AuthorizationStatus.PENDING)

