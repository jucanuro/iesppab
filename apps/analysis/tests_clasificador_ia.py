"""
Tests del dataset del clasificador de IA (sin red): plan de prompts,
limpieza, normalización compartida, segmentación, ventanas solapadas,
separación por documento, prueba de formato y el comando que pide textos
a Gemini (con `requests` simulado).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.analysis import ia_dataset
from apps.analysis.management.commands import exportar_dataset_ia as export
from apps.analysis.management.commands import generar_textos_ia as gen

PROSA = (
    "La comprensión lectora es una competencia que los estudiantes desarrollan "
    "de manera progresiva a lo largo de la escolaridad. Los docentes observan "
    "que muchos niños leen con fluidez pero no logran identificar la idea "
    "principal de los textos que trabajan en el aula. Esta situación se repite "
    "en diversas instituciones de la región y preocupa a las familias. "
)


def _prosa(palabras: int) -> str:
    texto = ""
    while len(texto.split()) < palabras:
        texto += PROSA
    return texto.strip()


_SUJETOS = ("los docentes", "las familias", "los estudiantes", "la directora", "el equipo",
            "las niñas", "los tutores", "la comunidad", "el acompañante", "los padres")
_VERBOS = ("observan", "reconocen", "describen", "valoran", "cuestionan", "organizan",
           "comparan", "registran", "discuten", "proponen")
_OBJETOS = ("la lectura en voz alta", "el juego simbólico", "la evaluación formativa",
            "el huerto escolar", "la convivencia del aula", "los cuentos andinos",
            "el material reciclado", "la escritura colaborativa", "las rutinas diarias",
            "el trabajo en equipo")
_CIERRES = ("durante las primeras semanas del año escolar", "con bastante entusiasmo",
            "aunque con algunas dudas iniciales", "en cada una de las sesiones",
            "cuando el tiempo de clase lo permite", "de manera cada vez más autónoma",
            "a partir de experiencias cercanas", "sin descuidar las demás áreas")


def _prosa_variada(palabras: int, semilla: int = 0) -> str:
    import random

    rng = random.Random(semilla)
    oraciones = []
    while sum(len(o.split()) for o in oraciones) < palabras:
        oraciones.append(
            f"{rng.choice(_SUJETOS).capitalize()} {rng.choice(_VERBOS)} "
            f"{rng.choice(_OBJETOS)} {rng.choice(_CIERRES)}, y luego "
            f"{rng.choice(_VERBOS)} {rng.choice(_OBJETOS)} {rng.choice(_CIERRES)}."
        )
    return " ".join(oraciones)


class PlanDePromptsTests(SimpleTestCase):
    def test_semilla_fija_da_el_mismo_plan(self):
        a = ia_dataset.build_plan(20, seed=1, prefix="x")
        b = ia_dataset.build_plan(20, seed=1, prefix="x")
        self.assertEqual([s.prompt for s in a], [s.prompt for s in b])

    def test_otra_semilla_cambia_el_plan(self):
        a = ia_dataset.build_plan(20, seed=1, prefix="x")
        b = ia_dataset.build_plan(20, seed=2, prefix="x")
        self.assertNotEqual([s.prompt for s in a], [s.prompt for s in b])

    def test_ids_estilos_y_longitudes(self):
        plan = ia_dataset.build_plan(60, seed=3, prefix="gemini_api")
        self.assertEqual(plan[0].id, "gemini_api_001")
        self.assertEqual(plan[-1].id, "gemini_api_060")
        self.assertEqual(
            {s.estilo for s in plan},
            {ia_dataset.STYLE_NORMAL, ia_dataset.STYLE_STUDENT, ia_dataset.STYLE_HUMANIZED},
        )
        self.assertTrue(all(800 <= s.palabras <= 1200 for s in plan))
        self.assertTrue(all(s.tema in s.prompt and s.seccion in s.prompt for s in plan))

    def test_estilo_de_los_prompts_de_chat(self):
        self.assertEqual(ia_dataset.chat_style(1), ia_dataset.STYLE_NORMAL)
        self.assertEqual(ia_dataset.chat_style(18), ia_dataset.STYLE_NORMAL)
        self.assertEqual(ia_dataset.chat_style(19), ia_dataset.STYLE_STUDENT)
        self.assertEqual(ia_dataset.chat_style(24), ia_dataset.STYLE_STUDENT)
        self.assertEqual(ia_dataset.chat_style(25), ia_dataset.STYLE_HUMANIZED)


class LimpiezaYNormalizacionTests(SimpleTestCase):
    def test_quita_markdown_y_frase_de_cortesia(self):
        texto = "¡Claro! Aquí tienes el texto:\n## Marco teórico\n**La lectura** es *clave*.\n* uno\n---\n"
        limpio = ia_dataset.clean_generated_text(texto)
        self.assertNotIn("Claro", limpio)
        self.assertNotIn("#", limpio)
        self.assertNotIn("*", limpio)
        self.assertNotIn("---", limpio)
        self.assertIn("La lectura es clave.", limpio)
        self.assertIn("- uno", limpio)

    def test_normalizacion_compartida(self):
        texto = (
            "2.1. Antecedentes\n"
            "Según Piaget (1961), el niño; en el 45,5 % de los casos: “juega” "
            "(ver Tabla 3) con la UGEL [2]. Además, se aplicó\n"
            "la prue- ba a 30 niños (Pérez y Ruiz, 2019, p. 4). Ver https://x.org/a.\n"
            "12\n"
        )
        salida = ia_dataset.normalize_segment_text(texto)
        self.assertEqual(
            salida,
            "según piaget, el niño en el de los casos juega con la ugel. "
            "además, se aplicó la prueba a niños. ver",
        )

    def test_quita_vinetas_simbolos_y_rachas_en_mayusculas(self):
        texto = " El estudio = muestra EDUCACIÓN DE LOS ESTUDIANTES que la escuela mejora."
        self.assertEqual(
            ia_dataset.normalize_segment_text(texto),
            "el estudio muestra que la escuela mejora.",
        )

    def test_detecta_palabras_partidas_por_el_pdf(self):
        limpio = _prosa(150)
        partido = limpio.replace("la escolaridad", "la e scolaridad").replace(
            "principal", "p rincipal"
        )
        self.assertFalse(ia_dataset.has_pdf_artifacts(limpio))
        self.assertTrue(ia_dataset.has_pdf_artifacts(partido))


class SegmentosTests(SimpleTestCase):
    def test_segmentos_de_prosa_entre_100_y_250_palabras(self):
        texto = "\n\n".join(_prosa_variada(120, i) for i in range(8))
        segmentos = ia_dataset.prose_segments(texto)
        self.assertGreaterEqual(len(segmentos), 3)
        for segmento in segmentos:
            palabras = len(segmento.split())
            self.assertGreaterEqual(palabras, ia_dataset.MIN_SEGMENT_WORDS)
            self.assertLessEqual(palabras, 260)
            self.assertEqual(segmento, segmento.lower())
            self.assertNotIn("\n", segmento)

    def test_las_listas_no_dan_segmentos(self):
        lista = "\n".join(f"- Elemento número {i} de la lista de materiales" for i in range(60))
        self.assertEqual(ia_dataset.prose_segments(lista), [])

    def test_parrafo_largo_se_parte_en_trozos_parejos(self):
        piezas = ia_dataset._split_long(_prosa(420))
        self.assertEqual(len(piezas), 2)
        self.assertTrue(all(150 <= len(p.split()) <= 260 for p in piezas))

    def test_ventanas_solapadas(self):
        segmentos = [_prosa(200), _prosa(200), _prosa(200)]
        ventanas = ia_dataset.overlapping_windows(segmentos, size=200, stride=100)
        self.assertGreater(len(ventanas), len(segmentos))
        for ventana in ventanas:
            self.assertGreaterEqual(len(ventana.split()), ia_dataset.MIN_SEGMENT_WORDS)
        primera, segunda = ventanas[0].split(), ventanas[1].split()
        self.assertTrue(set(primera[-50:]) & set(segunda[:120]))

    def test_pdf_con_texto_utf16_intercalado(self):
        pagina = "\x00E\x00l\x00 \x00d\x00o\x00c\x00e\x00n\x00t\x00e\x00 observa.\x07\x7f"
        with mock.patch("apps.documents.extractors.read_pdf_pages", return_value=[pagina]):
            self.assertEqual(ia_dataset.pdf_bytes_to_text(b"%PDF"), "El docente observa.")

    def test_texto_generado_pasa_por_el_pdf(self):
        texto = "Introducción\n\n" + "\n\n".join(_prosa_variada(130, i) for i in range(5))
        extraido = ia_dataset.generated_text_via_pdf(texto)
        self.assertIn("y luego", extraido)
        segmentos = ia_dataset.prose_segments(extraido)
        self.assertGreaterEqual(len(segmentos), 2)


class ExportacionTests(SimpleTestCase):
    def test_particiones_por_documento(self):
        docs = [f"d{i}" for i in range(10)]
        splits = export._assign_splits(docs, export.HUMAN_SPLITS)
        self.assertEqual(len(splits), 10)
        self.assertEqual(sum(1 for v in splits.values() if v == "train"), 7)
        self.assertEqual(sum(1 for v in splits.values() if v == "val"), 1)
        self.assertEqual(sum(1 for v in splits.values() if v == "test"), 2)

    def test_detecta_documentos_en_dos_particiones(self):
        rows = export._rows(["a"], 0, "humano_corpus", "doc1", "train", "")
        rows += export._rows(["b"], 0, "humano_corpus", "doc1", "test", "")
        rows += export._rows(["c"], 1, "gemini_api", "doc2", "train", "normal")
        self.assertEqual(export._document_leaks(rows), ["doc1"])

    def test_handle_del_repositorio(self):
        self.assertEqual(export._handle("https://hdl.handle.net/20.500.12840/5546"), "20.500.12840/5546")
        self.assertEqual(export._handle("upeu_2019_20.500.12840_2096.pdf"), "20.500.12840/2096")
        self.assertEqual(export._handle("sin handle"), "")

    def test_prueba_de_formato_no_separa_textos_equivalentes(self):
        rows = []
        for i in range(40):
            texto = ia_dataset.normalize_segment_text(_prosa(150 + i))
            rows += export._rows([texto], i % 2, "x", f"doc{i}", "train", "")
        resultado = export.format_probe(rows)
        self.assertLessEqual(resultado["auroc"], export.FORMAT_PROBE_MAX_AUROC)
        self.assertIn("comas", resultado["auroc_por_rasgo"])
        self.assertIn("desv_palabras_por_oracion", resultado["auroc_por_rasgo"])


class _Respuesta:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


def _ok(texto):
    return _Respuesta(200, {"candidates": [{"content": {"parts": [{"text": texto}]}}]})


def _cuota_diaria():
    return _Respuesta(
        429,
        {"error": {"message": "quota", "details": [{"violations": [
            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
        ]}]}},
    )


class GenerarTextosTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env.prueba"
        self.env.write_text("GEMINI_API_KEY=clave-de-prueba\n", encoding="utf-8")

    def _call(self, *args):
        call_command(
            "generar_textos_ia",
            "--salida", self.tmp.name,
            "--env-file", str(self.env),
            "--intervalo", "0",
            *args,
            stdout=mock.MagicMock(),
            stderr=mock.MagicMock(),
        )

    def test_solo_plan_no_llama_a_la_api(self):
        with mock.patch("requests.post") as post:
            self._call("--fuente", "claude", "--cantidad", "5", "--solo-plan")
        post.assert_not_called()
        plan = json.loads((Path(self.tmp.name) / "claude" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plan["textos"]), 5)
        self.assertEqual(plan["semilla"], ia_dataset.PLAN_SEED_CLAUDE)

    def test_genera_y_rota_de_modelo_al_agotar_la_cuota(self):
        respuestas = [_ok("Texto uno."), _cuota_diaria(), _ok("Texto dos.")]
        with mock.patch("requests.post", side_effect=respuestas) as post:
            self._call("--fuente", "gemini", "--cantidad", "2", "--modelos", "modelo-a,modelo-b")
        carpeta = Path(self.tmp.name) / "gemini"
        self.assertEqual((carpeta / "gemini_api_001.txt").read_text(encoding="utf-8"), "Texto uno.")
        self.assertEqual((carpeta / "gemini_api_002.txt").read_text(encoding="utf-8"), "Texto dos.")
        modelos = json.loads((carpeta / "modelos.json").read_text(encoding="utf-8"))
        self.assertEqual(modelos, {"gemini_api_001": "modelo-a", "gemini_api_002": "modelo-b"})
        urls = [c.args[0] for c in post.call_args_list]
        self.assertIn("modelo-b", urls[-1])
        # La clave va en la cabecera, nunca en la URL.
        self.assertTrue(all("clave-de-prueba" not in url for url in urls))

    def test_retoma_sin_repetir_lo_ya_generado(self):
        with mock.patch("requests.post", side_effect=[_ok("A"), _cuota_diaria()]):
            self._call("--fuente", "gemini", "--cantidad", "3", "--modelos", "solo")
        with mock.patch("requests.post", side_effect=[_ok("B"), _ok("C")]) as post:
            self._call("--fuente", "gemini", "--cantidad", "3", "--modelos", "solo")
        self.assertEqual(post.call_count, 2)
        carpeta = Path(self.tmp.name) / "gemini"
        self.assertEqual(len(list(carpeta.glob("gemini_api_*.txt"))), 3)

    def test_sin_clave_falla_con_mensaje(self):
        self.env.write_text("OTRA=1\n", encoding="utf-8")
        with self.assertRaisesMessage(Exception, "No hay GEMINI_API_KEY"):
            self._call("--fuente", "gemini", "--cantidad", "1")

    def test_retry_delay(self):
        self.assertEqual(gen._retry_delay({"details": [{"retryDelay": "30s"}]}), 31.0)
        self.assertIsNone(gen._retry_delay({}))
