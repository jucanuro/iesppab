"""
T09 · Construye el banco IA v3: valida el manifiesto, genera mixtos y exporta JSONL
por partición (design §3). Con `--para-colab` exporta solo train y val y
aborta si algún texto exportado tiene `publico=false` (REQ-IA-16). No hace commit.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.analysis.ia_v3.banco import MIXTOS_POR_OBJETIVO, construir_banco
from apps.analysis.ia_v3.exceptions import IABankError
from apps.analysis.ia_v3.manifest import IA_V3_SEED

DEFAULT_RAIZ = Path(settings.BASE_DIR) / "eval_ia"
DEFAULT_MANIFIESTO = Path(settings.BASE_DIR) / "apps/analysis/evaluation/ia_v3/manifiesto.json"


class Command(BaseCommand):
    help = (
        "Construye el banco IA v3 (textos puros + mixtos) y lo exporta como JSONL por "
        "partición. --para-colab: solo train y val, y solo textos públicos."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--manifiesto", default=str(DEFAULT_MANIFIESTO))
        parser.add_argument("--raiz", default=str(DEFAULT_RAIZ), help="Carpeta eval_ia/.")
        parser.add_argument("--salida", default=None,
                            help="Carpeta de salida (por defecto eval_ia/banco_v3[_colab]).")
        parser.add_argument("--semilla", type=int, default=IA_V3_SEED)
        parser.add_argument("--mixtos-por-objetivo", type=int, default=MIXTOS_POR_OBJETIVO)
        parser.add_argument("--para-colab", action="store_true",
                            help="Exporta solo train y val, y solo textos públicos (REQ-IA-16).")

    def handle(self, *args, **options) -> None:
        raiz = Path(options["raiz"])
        para_colab = options["para_colab"]
        salida = Path(options["salida"] or raiz / ("banco_v3_colab" if para_colab else "banco_v3"))
        try:
            res = construir_banco(
                options["manifiesto"], raiz, salida, options["semilla"],
                para_colab=para_colab, por_objetivo=options["mixtos_por_objetivo"],
            )
        except IABankError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Banco IA v3 en {salida} (semilla {options['semilla']}"
                          f"{', para Colab' if para_colab else ''})")
        for particion, ruta in sorted(res.archivos.items()):
            c = res.conteos[particion]
            self.stdout.write(f"  {particion:11s} puros={c['puros']:4d} mixtos={c['mixtos']:4d} "
                              f"sha256={res.hashes[particion][:16]}… {ruta.name}")
        for o in res.omitidos:
            self.stdout.write(self.style.WARNING(f"  mixto omitido: {o}"))
        self.stdout.write(f"hash_banco: {res.hash_banco}")
