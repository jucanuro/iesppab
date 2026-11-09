"""
Diagnóstico del clasificador de IA por segmento (no modifica la lógica).

Uso:
    source venv/bin/activate
    python scripts/diag_ia.py RUTA.docx|RUTA.pdf [--sin-reduccion] [--cuantizar]

Extrae el texto con el mismo extractor/filtro que el análisis, parte en trozos
como el clasificador e imprime número, palabras y probabilidad de cada uno.

Modos de carga (el código de producción solo tiene la reducción de memoria,
sin cuantización):
  por defecto       producción: low_cpu_mem_usage=True, lotes de 2, 2 hilos
  --sin-reduccion   carga normal: low_cpu_mem_usage=False, lotes de 32, hilos por defecto
  --cuantizar       cuantización dinámica int8 (solo experimento, no existe en producción)
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from apps.analysis.engines import ai_classifier as clf  # noqa: E402
from apps.analysis.engines.text_filters import AcademicTextFilter  # noqa: E402
from apps.analysis.management.commands.exportar_dataset_ia import _file_document  # noqa: E402
from apps.documents.extractors import DocumentTextExtractor  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ruta")
    parser.add_argument("--sin-reduccion", action="store_true")
    parser.add_argument("--cuantizar", action="store_true")
    args = parser.parse_args()

    modo = "producción: low_cpu_mem_usage=True, lotes=2, hilos=2"
    if args.sin_reduccion:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        clf.BATCH_SIZE = 32
        modo = "SIN reducción: low_cpu_mem_usage=False, lotes=32, hilos por defecto"

        def _load_plain(model_dir):
            config = clf._read_config(model_dir)
            model = AutoModelForSequenceClassification.from_pretrained(
                model_dir, low_cpu_mem_usage=False
            ).eval()
            clf._loaded[model_dir] = (
                AutoTokenizer.from_pretrained(model_dir), model,
                float(config["umbral"]), int(config.get("max_length", 384)), torch,
            )
            return clf._loaded[model_dir]

        clf._load = _load_plain
    elif args.cuantizar:
        import torch
        orig = clf._load

        def _load_q(model_dir):
            tok, model, thr, ml, t = orig(model_dir)
            q = torch.quantization.quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8)
            clf._loaded[model_dir] = (tok, q, thr, ml, t)
            return clf._loaded[model_dir]

        clf._load = _load_q
        modo = "cuantización dinámica int8 (experimento)"

    path = Path(args.ruta)
    text_filter = AcademicTextFilter()
    raw = DocumentTextExtractor().extract(_file_document(path)).content
    content = text_filter.filter_for_similarity(content=raw).content or raw

    classifier = clf.SegmentAIClassifier()
    result = classifier.analyze(content, excluded_ranges=text_filter.find_quoted_ranges(content))
    pieces = result.pieces
    umbral = result.threshold

    print(f"Archivo: {path.name}\nModo de carga: {modo}\nUmbral: {umbral}\n")
    print(f"{'nº':>4} {'palabras':>8} {'probabilidad':>14}")
    for i, p in enumerate(pieces, 1):
        marca = " *" if p.probability >= umbral else ""
        print(f"{i:>4} {p.word_count:>8} {p.probability:>14.6f}{marca}")

    probs = [p.probability for p in pieces]
    sobre = sum(p >= umbral for p in probs)
    print(f"\nTotal de segmentos: {len(pieces)}")
    print(f"Sobre el umbral: {sobre} ({100 * sobre / len(pieces):.2f} % de segmentos)" if pieces else "Sin segmentos")
    print(f"% IA (por palabras): {result.ai_percent}")
    if pieces:
        print(f"Probabilidad media: {sum(probs) / len(probs):.6f}  mínima: {min(probs):.6f}")
    print(f"Modo de carga: {modo}")


if __name__ == "__main__":
    main()
