"""Tests (sin red) de la preparación de humanos de Wikipedia para el clasificador v2."""

from __future__ import annotations

import io
import tempfile
from html import escape
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.analysis import wikipedia_corpus as wiki

PARRAFO = (
    "La pedagogía es la ciencia que estudia los métodos y las técnicas utilizadas en la "
    "enseñanza, así como los procesos mediante los cuales las personas adquieren conocimientos "
    "a lo largo de la vida. Su desarrollo histórico estuvo ligado a la filosofía y a la psicología, "
    "y hoy dialoga con la sociología y con las neurociencias para explicar el aprendizaje."
)

WIKITEXT = f"""{{{{Ficha de disciplina|nombre=Pedagogía}}}}
'''Pedagogía''' es una disciplina.<ref>Cita de prueba</ref>

== Historia ==
{PARRAFO} [[Archivo:Foto.jpg|miniaturadeimagen|Un pie de foto que no debe quedar]]

* elemento de lista que no debe quedar
* otro elemento

{{| class="wikitable"
|-
| celda || otra celda
|}}

=== Siglo XX ===
{PARRAFO}

== Véase también ==
* [[Didáctica]]

== Referencias ==
{PARRAFO} texto posterior a referencias que no debe quedar.

[[Categoría:Pedagogía]]
"""


def _page(title, text, ns="0", redirect=False):
    red = '<redirect title="X" />' if redirect else ""
    return (f"<page><title>{title}</title><ns>{ns}</ns>{red}"
            f"<revision><text>{escape(text)}</text></revision></page>")


class ClasificacionTests(SimpleTestCase):
    def test_extrae_categorias_y_tematica(self):
        cats = wiki.extract_categories("[[Categoría:Pedagogía]] [[Categoría:Historia de la educación|*]]")
        self.assertEqual(cats, ["Pedagogía", "Historia de la educación"])
        self.assertEqual(wiki.classify_theme(["Pedagogía"]), "pedagogia")
        self.assertEqual(wiki.classify_theme(["Psicología del desarrollo"]), "psicologia")
        self.assertEqual(wiki.classify_theme(["Historia de Perú"]), "historia")

    def test_biografias_y_sin_categoria_no_entran(self):
        self.assertIsNone(wiki.classify_theme(["Pedagogos de Perú", "Nacidos en 1900"]))
        self.assertIsNone(wiki.classify_theme([]))
        self.assertIsNone(wiki.classify_theme(["Cocina"]))


class ProsaTests(SimpleTestCase):
    def setUp(self):
        self.prose = wiki.wikitext_to_prose(WIKITEXT)

    def test_conserva_la_prosa(self):
        self.assertIn("La pedagogía es la ciencia", self.prose)
        self.assertEqual(self.prose.count("La pedagogía es la ciencia"), 2)

    def test_quita_listas_tablas_referencias_y_cierre(self):
        for ruido in ("elemento de lista", "celda", "pie de foto", "Cita de prueba",
                      "posterior a referencias", "==", "{{", "[["):
            self.assertNotIn(ruido, self.prose)

    def test_parrafos_cortos_se_descartan(self):
        self.assertNotIn("es una disciplina", self.prose)


class VolcadoTests(SimpleTestCase):
    def _dump(self):
        xml = ("<mediawiki xmlns='http://www.mediawiki.org/xml/export-0.11/'>"
               + _page("Pedagogía", "a")
               + _page("Redirección", "b", redirect=True)
               + _page("Wikipedia:Café", "c", ns="4")
               + _page("Otra", "d")
               + "</mediawiki>")
        return io.BytesIO(xml.encode())

    def test_solo_articulos_de_contenido(self):
        self.assertEqual([t for t, _ in wiki.iter_dump_pages(self._dump())], ["Pedagogía", "Otra"])

    def test_scan_filtra_tematica_largo_y_excluidos(self):
        largo = WIKITEXT + "x" * wiki.MIN_WIKITEXT_CHARS
        xml = ("<mediawiki>" + _page("Pedagogía", largo) + _page("Corto", WIKITEXT)
               + _page("Excluido", largo) + "</mediawiki>")
        res = wiki.scan_dump(io.BytesIO(xml.encode()), size_per_theme=5, seed=1,
                             exclude_titles={"excluido"})
        self.assertEqual([c.title for c in res.items["pedagogia"]], ["Pedagogía"])

    def test_reservorio_acotado_y_reproducible(self):
        def run():
            res = wiki.ThemeReservoir(3, seed=7)
            for i in range(50):
                res.offer(wiki.Candidate(f"t{i}", "historia", ""))
            return [c.title for c in res.items["historia"]]
        self.assertEqual(len(run()), 3)
        self.assertEqual(run(), run())

    def test_seleccion_balanceada_descarta_articulos_cortos(self):
        res = wiki.ThemeReservoir(5, seed=1)
        largo = (PARRAFO + "\n\n") * 30
        for theme in ("historia", "ciencia"):
            res.offer(wiki.Candidate(f"{theme}-largo", theme, largo))
        res.offer(wiki.Candidate("corto", "ciencia", PARRAFO))
        chosen = wiki.select_balanced(res, 10, seed=1)
        self.assertEqual({a["titulo"] for a in chosen}, {"historia-largo", "ciencia-largo"})


class ComandoTests(SimpleTestCase):
    def test_sin_dump_falla_con_mensaje(self):
        from django.core.management.base import CommandError
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CommandError):
                call_command("preparar_wikipedia_v2", dump=str(Path(tmp) / "no.bz2"))


class ExportadorV2Tests(SimpleTestCase):
    def test_wikipedia_reservada_estratificada_y_sin_mezcla(self):
        from apps.analysis.management.commands import exportar_dataset_ia_v2 as v2
        themes = ["historia", "ciencia", "psicologia", "educacion"]
        articles = [{"titulo": f"a{i}", "tematica": themes[i % 4]} for i in range(100)]
        splits = v2.split_wikipedia(articles, 20, seed=3)
        self.assertEqual(splits.count("test_wiki"), 20)
        self.assertEqual(splits.count("train"), 80)
        reserved_themes = {articles[i]["tematica"] for i, s in enumerate(splits) if s == "test_wiki"}
        self.assertEqual(reserved_themes, set(themes))
        self.assertEqual(splits, v2.split_wikipedia(articles, 20, seed=3))

    def test_ventana_y_slug(self):
        import random
        from apps.analysis.management.commands import exportar_dataset_ia_v2 as v2
        text = "\n\n".join(f"palabra{i} " * 100 for i in range(40))
        window = v2._window(text, 500, random.Random(1))
        self.assertLessEqual(len(window.split()), 700)
        self.assertEqual(v2._window("corto texto", 500, random.Random(1)), "corto texto")
        self.assertEqual(v2._slug("Pedagogía (ciencia)"), "pedagogia_ciencia")

    def test_tope_por_documento(self):
        from apps.analysis.management.commands import exportar_dataset_ia_v2 as v2
        rows = [{"documento": d, "texto": str(i)} for d in "ab" for i in range(30)]
        kept = v2.Command()._capped(rows, 4)
        self.assertEqual(len(kept), 8)

    def test_wikipedia_nunca_cae_en_train_si_esta_reservada(self):
        from apps.analysis.management.commands import exportar_dataset_ia_v2 as v2
        articles = [{"titulo": f"a{i}", "tematica": "historia"} for i in range(10)]
        splits = v2.split_wikipedia(articles, 3, seed=1)
        self.assertEqual({s for s in splits}, {"train", "test_wiki"})
