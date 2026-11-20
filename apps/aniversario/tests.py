from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from . import services
from .models import MAX_FOTOS, VIDEO_MAX_MB, Candidato, Concurso, FotoCandidato, Voto

HOST = "aniversario.testserver"
AJUSTES = dict(
    ANIVERSARIO_HOST=HOST,
    ALLOWED_HOSTS=[HOST, "testserver", "originalidad.testserver"],
    ANIVERSARIO_RATE_POR_MINUTO=100,
)


def crear_concurso(**extra) -> Concurso:
    ahora = timezone.now()
    datos = dict(
        nombre="Aniversario 2026",
        fecha_inicio=ahora - timedelta(days=1),
        fecha_cierre=ahora + timedelta(days=1),
        max_votos_por_ip=1,
    )
    datos.update(extra)
    return Concurso.objects.create(**datos)


@override_settings(**AJUSTES)
class BaseAniversario(TestCase):
    def setUp(self):
        caches["aniversario"].clear()
        self.concurso = crear_concurso()
        self.miss = Candidato.objects.create(
            concurso=self.concurso, categoria="miss", numero=1, nombres="Sofía Delgado",
            ciclo="I", carrera="Educación Secundaria",
        )
        self.miss2 = Candidato.objects.create(
            concurso=self.concurso, categoria="miss", numero=2, nombres="Valeria Quispe",
            ciclo="II", carrera="Educación Primaria",
        )
        self.mister = Candidato.objects.create(
            concurso=self.concurso, categoria="mister", numero=1, nombres="Bruno Silva",
            ciclo="I", carrera="Educación Secundaria",
        )
        self.client.defaults["HTTP_HOST"] = HOST

    def votar(self, client=None, miss=None, mister=None, ip="200.1.1.1"):
        client = client or self.client
        return client.post(
            "/votar/confirmar/",
            {"miss": (miss or self.miss).pk, "mister": (mister or self.mister).pk},
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_REAL_IP=ip,
        )

    def nuevo_cliente(self):
        c = self.client_class()
        c.defaults["HTTP_HOST"] = HOST
        return c


class VotoTests(BaseAniversario):
    def test_voto_valido(self):
        self.client.get("/votar/")  # recibe la cookie de dispositivo
        r = self.votar()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "¡Gracias por votar!")
        voto = Voto.objects.get()
        self.assertRegex(voto.codigo, r"^AN-[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertContains(r, voto.codigo)
        self.assertEqual(voto.ip, "200.1.1.1")
        self.assertEqual((voto.miss, voto.mister), (self.miss, self.mister))
        self.assertIn(services.COOKIE_DISPOSITIVO, self.client.cookies)
        cookie = self.client.cookies[services.COOKIE_DISPOSITIVO]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_voto_sin_cookie_previa_tambien_crea_token(self):
        r = self.votar()
        self.assertContains(r, "¡Gracias por votar!")
        self.assertEqual(Voto.objects.count(), 1)

    def test_mismo_token_no_vota_dos_veces_y_ve_su_comprobante(self):
        self.votar()
        voto = Voto.objects.get()
        r = self.votar(miss=self.miss2, ip="200.9.9.9")
        self.assertEqual(Voto.objects.count(), 1)
        self.assertContains(r, voto.codigo)
        for ruta in ("/votar/", f"/votar/mister/?miss={self.miss.pk}", "/votar/confirmar/"):
            self.assertContains(self.client.get(ruta), voto.codigo)

    def test_limite_por_ip(self):
        self.votar(ip="10.0.0.5")
        r = self.votar(client=self.nuevo_cliente(), ip="10.0.0.5")
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "máximo de votos", status_code=400)
        self.assertEqual(Voto.objects.count(), 1)

    def test_limite_por_ip_configurable(self):
        self.concurso.max_votos_por_ip = 2
        self.concurso.save()
        self.votar(ip="10.0.0.5")
        self.votar(client=self.nuevo_cliente(), ip="10.0.0.5")
        self.votar(client=self.nuevo_cliente(), ip="10.0.0.5")
        self.assertEqual(Voto.objects.count(), 2)

    def test_fuera_de_fecha(self):
        ahora = timezone.now()
        for inicio, cierre, texto in (
            (ahora + timedelta(hours=1), ahora + timedelta(days=1), "aún no"),
            (ahora - timedelta(days=2), ahora - timedelta(hours=1), "cerró"),
        ):
            Concurso.objects.filter(pk=self.concurso.pk).update(fecha_inicio=inicio, fecha_cierre=cierre)
            r = self.votar(client=self.nuevo_cliente())
            self.assertEqual(r.status_code, 400)
            self.assertContains(r, texto, status_code=400)
            self.assertContains(self.client.get("/votar/"), texto)
        self.assertEqual(Voto.objects.count(), 0)

    def test_categoria_cruzada(self):
        r = self.votar(miss=self.mister, mister=self.miss)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Voto.objects.count(), 0)
        with self.assertRaises(ValidationError):
            Voto(concurso=self.concurso, miss=self.mister, mister=self.miss,
                 codigo="AN-X", token_dispositivo="t").full_clean()

    def test_ambos_obligatorios(self):
        r = self.client.post("/votar/confirmar/", {"miss": self.miss.pk})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Voto.objects.count(), 0)

    def test_post_requiere_csrf(self):
        c = self.client_class(enforce_csrf_checks=True)
        c.defaults["HTTP_HOST"] = HOST
        r = c.post("/votar/confirmar/", {"miss": self.miss.pk, "mister": self.mister.pk})
        self.assertContains(r, "El formulario caducó", status_code=403)
        self.assertEqual(Voto.objects.count(), 0)

    def test_integrity_error_por_carrera_devuelve_comprobante(self):
        self.votar()
        token = services.leer_token(self.client.cookies and _req_con_cookies(self.client))
        original = services.voto_del_dispositivo
        llamadas = {"n": 0}

        def primera_vez_none(t):
            llamadas["n"] += 1
            return None if llamadas["n"] == 1 else original(t)

        with mock.patch.object(services, "voto_del_dispositivo", side_effect=primera_vez_none):
            with self.assertRaises(services.VotoDuplicadoError):
                services.VotoService(self.concurso).registrar(services.DatosVoto(
                    miss_id=self.miss.pk, mister_id=self.mister.pk, ip="1.1.1.1", token=token,
                ))
        self.assertEqual(Voto.objects.count(), 1)

    def test_rate_limit(self):
        with override_settings(ANIVERSARIO_RATE_POR_MINUTO=2):
            for _ in range(3):
                r = self.client.post("/votar/confirmar/", {"miss": "x"}, REMOTE_ADDR="127.0.0.1",
                                     HTTP_X_REAL_IP="9.9.9.9")
        self.assertContains(r, "Demasiados intentos", status_code=400)


def _req_con_cookies(client):
    req = RequestFactory().get("/")
    req.COOKIES = {k: v.value for k, v in client.cookies.items()}
    return req


class FlujoYConteoTests(BaseAniversario):
    def test_paginas_publicas(self):
        self.assertContains(self.client.get("/"), "Sofía Delgado")
        r = self.client.get(f"/candidato/{self.miss.pk}/")
        self.assertContains(r, 'property="og:title"')
        self.assertContains(r, "wa.me")
        self.assertEqual(r["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertContains(self.client.get("/votar/"), 'name="miss"')
        r = self.client.get("/votar/mister/")
        self.assertRedirects(r, "/votar/", fetch_redirect_response=False)
        self.assertContains(self.client.get(f"/votar/mister/?miss={self.miss.pk}"), 'name="mister"')
        r = self.client.get(f"/votar/confirmar/?miss={self.miss.pk}&mister={self.mister.pk}")
        self.assertContains(r, "Confirma tu voto")
        self.assertContains(r, "csrfmiddlewaretoken")

    def test_conteo_agregado(self):
        self.votar(ip="1.1.1.1")
        self.votar(client=self.nuevo_cliente(), ip="1.1.1.2")
        self.votar(client=self.nuevo_cliente(), miss=self.miss2, ip="1.1.1.3")
        datos = services.calcular_conteo(self.concurso)
        self.assertLessEqual({"votos": 2, "pct": 67, "puesto": 1}.items(), datos[self.miss.pk].items())
        self.assertLessEqual({"votos": 1, "pct": 33, "puesto": 2}.items(), datos[self.miss2.pk].items())
        self.assertLessEqual({"votos": 3, "pct": 100, "puesto": 1}.items(), datos[self.mister.pk].items())
        caches["aniversario"].clear()
        api = self.client.get("/api/conteo/").json()
        self.assertEqual(api["candidatos"][str(self.miss.pk)]["votos"], 2)

    def test_conteo_oculto(self):
        self.concurso.mostrar_conteo = False
        self.concurso.save()
        self.assertEqual(self.client.get("/api/conteo/").json(), {"visible": False, "candidatos": {}})
        self.assertNotContains(self.client.get("/"), "data-vt")

    def test_sin_concurso_404_propio(self):
        Concurso.objects.update(activo=False)
        r = self.client.get("/")
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Página no encontrada", status_code=404)


@override_settings(**AJUSTES)
class ContadorYEstadoTests(BaseAniversario):
    def test_mi_voto_se_ve_al_instante_aunque_el_conteo_este_cacheado(self):
        api = self.client.get("/api/conteo/").json()  # deja el conteo en caché con 0 votos
        self.assertEqual(api["total"], 0)
        self.votar()
        api = self.client.get("/api/conteo/").json()
        self.assertEqual(api["total"], 1)
        self.assertEqual(api["candidatos"][str(self.miss.pk)], {"votos": 1, "pct": 100, "puesto": 1})
        self.assertEqual(api["candidatos"][str(self.miss2.pk)]["puesto"], 2)
        self.assertNotIn("cat", api["candidatos"][str(self.miss.pk)])

    def test_total_en_el_inicio(self):
        self.votar()
        r = self.client.get("/")
        self.assertContains(r, "data-total>1<")
        self.assertContains(r, "votos emitidos")

    def test_estado_ya_votaste_fijo_en_todo_el_sitio(self):
        r = self.client.get("/")
        self.assertNotContains(r, "Ya votaste")
        self.votar()
        codigo = Voto.objects.get().codigo
        r = self.client.get("/")
        self.assertContains(r, "Ver mi comprobante")
        self.assertContains(r, codigo)
        self.assertContains(r, "Tu voto ✓", count=2)
        r = self.client.get(f"/candidato/{self.miss.pk}/")
        self.assertContains(r, "tiene tu voto")
        self.assertNotContains(r, "Elegir a ")
        r = self.client.get(f"/candidato/{self.miss2.pk}/")
        self.assertContains(r, "Ya votaste")
        # Otro equipo no ve el estado
        self.assertNotContains(self.nuevo_cliente().get("/"), codigo)

    def test_video_vertical_con_diseno_de_reserva(self):
        r = self.client.get("/")
        self.assertContains(r, "vd-vacio")
        r = self.client.get(f"/candidato/{self.miss.pk}/")
        self.assertContains(r, "vd-vacio")
        self.assertContains(r, "Muy pronto")


@override_settings(**AJUSTES)
class HostTests(TestCase):
    def test_host_aniversario_no_expone_originalidad(self):
        for ruta in ("/admin/", "/admin/login/", "/documentos/subir/", "/manual/", "/login/"):
            r = self.client.get(ruta, HTTP_HOST=HOST)
            self.assertEqual(r.status_code, 404, ruta)

    def test_host_originalidad_sigue_igual(self):
        r = self.client.get("/admin/", HTTP_HOST="originalidad.testserver")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/admin/login/", r["Location"])
        self.assertEqual(self.client.get("/admin/login/", HTTP_HOST="originalidad.testserver").status_code, 200)
        r = self.client.get("/votar/", HTTP_HOST="originalidad.testserver")
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/", HTTP_HOST="originalidad.testserver")
        self.assertEqual(r.headers.get("Referrer-Policy"), "same-origin")

    @override_settings(ANIVERSARIO_HOST="")
    def test_sin_host_configurado_no_hace_nada(self):
        self.assertEqual(self.client.get("/votar/", HTTP_HOST=HOST).status_code, 404)


class IpTests(TestCase):
    def setUp(self):
        self.rf = RequestFactory()

    def test_proxy_de_confianza(self):
        req = self.rf.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="181.65.1.2")
        self.assertEqual(services.obtener_ip(req), "181.65.1.2")
        req = self.rf.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4, 181.65.1.3")
        self.assertEqual(services.obtener_ip(req), "181.65.1.3")

    def test_socket_unix_sin_remote_addr(self):
        req = self.rf.get("/", REMOTE_ADDR="", HTTP_X_REAL_IP="181.65.1.2")
        self.assertEqual(services.obtener_ip(req), "181.65.1.2")

    def test_cliente_no_confiable_no_falsifica(self):
        req = self.rf.get("/", REMOTE_ADDR="190.5.5.5", HTTP_X_REAL_IP="1.1.1.1",
                          HTTP_X_FORWARDED_FOR="1.1.1.1")
        self.assertEqual(services.obtener_ip(req), "190.5.5.5")

    def test_valor_basura(self):
        req = self.rf.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="no-es-ip")
        self.assertEqual(services.obtener_ip(req), "127.0.0.1")


def _png(w=3000, h=2000):
    from PIL import Image

    buf = BytesIO()
    Image.new("RGBA", (w, h), (200, 10, 10, 255)).save(buf, "PNG")
    return SimpleUploadedFile("a.png", buf.getvalue(), content_type="image/png")


class MediaTestCase(TestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, True)
        ajuste = override_settings(MEDIA_ROOT=self.media)
        ajuste.enable()
        self.addCleanup(ajuste.disable)
        self.concurso = crear_concurso()
        self.cand = Candidato.objects.create(
            concurso=self.concurso, categoria="miss", numero=1, nombres="Ana Ruiz", ciclo="I", carrera="X",
        )


class FotoTests(MediaTestCase):
    def test_foto_se_optimiza(self):
        from PIL import Image

        f = FotoCandidato(candidato=self.cand, imagen=_png())
        f.full_clean()
        f.save()
        self.assertTrue(f.imagen.name.startswith("aniversario/candidatos/"))
        self.assertTrue(f.imagen.name.endswith(".jpg"))
        with Image.open(f.imagen.path) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(max(img.size), 1200)

    def test_archivo_falso_se_rechaza(self):
        f = FotoCandidato(candidato=self.cand,
                          imagen=SimpleUploadedFile("a.jpg", b"<?php echo 1; ?>", content_type="image/jpeg"))
        with self.assertRaises(ValidationError):
            f.full_clean()

    def test_maximo_cinco_fotos(self):
        for i in range(MAX_FOTOS):
            FotoCandidato.objects.create(candidato=self.cand, imagen=_png(40, 50), orden=i)
        sexta = FotoCandidato(candidato=self.cand, imagen=_png(40, 50))
        with self.assertRaises(ValidationError):
            sexta.full_clean()

    def test_orden_y_borrado_de_archivo(self):
        import os

        b = FotoCandidato.objects.create(candidato=self.cand, imagen=_png(40, 50), orden=2)
        a = FotoCandidato.objects.create(candidato=self.cand, imagen=_png(40, 50), orden=1)
        self.assertEqual([f.pk for f in self.cand.fotos], [a.pk, b.pk])
        self.assertEqual(self.cand.foto_principal, a)
        ruta = a.imagen.path
        self.cand.delete()  # cascada
        self.assertFalse(os.path.exists(ruta))


class VideoTests(MediaTestCase):
    def test_video_falso_se_rechaza(self):
        self.cand.video_archivo = SimpleUploadedFile("v.mp4", b"esto no es video" * 100, content_type="video/mp4")
        with self.assertRaises(ValidationError) as ctx:
            self.cand.full_clean()
        self.assertIn("video_archivo", ctx.exception.message_dict)

    def test_video_demasiado_grande(self):
        archivo = SimpleUploadedFile("v.mp4", b"\x00\x00\x00\x18ftypmp42" + b"0" * 100, content_type="video/mp4")
        archivo.size = (VIDEO_MAX_MB + 1) * 1024 * 1024
        self.cand.video_archivo = archivo
        with self.assertRaises(ValidationError):
            self.cand.full_clean()

    def test_video_mp4_valido(self):
        cabecera = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 200
        self.cand.video_archivo = SimpleUploadedFile("v.mp4", cabecera, content_type="video/mp4")
        self.cand.full_clean()


@override_settings(**AJUSTES)
class PaginasConFotosTests(MediaTestCase):
    def setUp(self):
        super().setUp()
        caches["aniversario"].clear()
        for i in range(3):
            FotoCandidato.objects.create(candidato=self.cand, imagen=_png(40, 50), orden=i)
        self.client.defaults["HTTP_HOST"] = HOST

    def test_carrusel_en_tarjeta_y_perfil(self):
        r = self.client.get("/")
        self.assertContains(r, "data-car", count=1)
        self.assertContains(r, 'class="puntos"')
        self.assertEqual(r.content.decode().count("/media/aniversario/candidatos/"), 3)
        r = self.client.get(f"/candidato/{self.cand.pk}/")
        self.assertContains(r, "1 / 3")
        self.assertContains(r, "data-ir=", count=3)
        self.assertContains(r, 'property="og:image"')

    def test_video_subido_se_muestra_con_reproductor(self):
        Candidato.objects.filter(pk=self.cand.pk).update(video_archivo="aniversario/videos/x.mp4")
        r = self.client.get(f"/candidato/{self.cand.pk}/")
        self.assertContains(r, "<video controls")

    def test_video_enlace_youtube(self):
        Candidato.objects.filter(pk=self.cand.pk).update(video_url="https://youtu.be/abc123XYZ_-")
        r = self.client.get(f"/candidato/{self.cand.pk}/")
        self.assertContains(r, "youtube-nocookie.com/embed/abc123XYZ_-")


    def test_encuadre_de_foto(self):
        FotoCandidato.objects.filter(candidato=self.cand, orden=0).update(ajuste="contain", posicion="center top")
        r = self.client.get(f"/candidato/{self.cand.pk}/")
        self.assertContains(r, "object-fit:contain;object-position:center top")
        self.assertContains(r, 'class="fondo"')


class AdminTests(MediaTestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.admin = get_user_model().objects.create_superuser(
            username="root", email="root@example.com", password="x-Clave-123"
        )
        self.client.force_login(self.admin)

    def test_admin_candidato_con_fotos_en_linea(self):
        r = self.client.get(f"/admin/aniversario/candidato/{self.cand.pk}/change/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "fotos_rel-TOTAL_FORMS")
        self.assertContains(r, 'name="fotos_rel-MAX_NUM_FORMS" value="5"')
        self.assertEqual(self.client.get("/admin/aniversario/candidato/").status_code, 200)

    def test_exportar_csv(self):
        r = self.client.post("/admin/aniversario/concurso/", {
            "action": "exportar_resultados", "_selected_action": [self.concurso.pk],
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ana Ruiz", r.content.decode("utf-8-sig"))
