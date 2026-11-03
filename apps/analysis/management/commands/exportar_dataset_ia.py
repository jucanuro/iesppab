from __future__ import annotations

import json
import random
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from types import SimpleNamespace

from apps.analysis import ia_dataset
from apps.analysis.models import OaiFullText, OaiFullTextChunk, OaiFullTextStatus
from apps.documents.extractors import DocumentTextExtractor

BASE = Path(settings.BASE_DIR)
EVAL_DIR = BASE / "eval_ia"
DEFAULT_OUTPUT = BASE / "dataset"

HUMAN_MAX_SEGMENTS = 2000
HUMAN_SPLITS = (("train", 0.70), ("val", 0.10), ("test", 0.20))
API_VAL_RATIO = 0.15
SPLIT_SEED = 20261104
MAX_YEAR = 2021
FORMAT_PROBE_MAX_AUROC = 0.65

LABEL_HUMAN = 0
LABEL_AI = 1

# El corpus se cosechó filtrando títulos de educación; este filtro solo
# descarta los pocos que no lo son (p. ej. gestión de proyectos de TI).
EDUCATION_TITLE_RE = re.compile(
    r"educaci|docente|maestr[oa]s|pedag|instituci[oó]n educativa|primaria|"
    r"secundaria|nivel inicial|aprendizaje|enseñanza|did[aá]ctic|"
    r"rendimiento acad|lectora|lectoescrit|psicomotr|\bI\.\s?E\b|escolar",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(\d+\.\d+\.\d+)[/_](\d+)")


class Command(BaseCommand):
    help = (
        "Exporta el dataset del clasificador de IA (JSONL en dataset/, fuera de "
        "git): segmentos de prosa de 150-250 palabras, separados por documento, "
        "y corre la prueba de 'solo formato' (AUROC debe ser <= 0,65)."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--salida", default=str(DEFAULT_OUTPUT))
        parser.add_argument("--max-humanos", type=int, default=HUMAN_MAX_SEGMENTS)

    def handle(self, *args, **options) -> None:
        out_dir = Path(options["salida"])
        out_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []

        control_handles = self._control_handles()
        rows += self._control_rows()
        rows += self._corpus_rows(control_handles, options["max_humanos"])
        rows += self._api_rows("gemini")
        rows += self._api_rows("claude")
        rows += self._chat_rows()

        path = out_dir / "segmentos.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        summary = self._summary(rows)
        summary["prueba_formato"] = format_probe(rows)
        (out_dir / "resumen.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        with zipfile.ZipFile(out_dir / "dataset_clasificador_ia.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, "segmentos.jsonl")
            zf.write(out_dir / "resumen.json", "resumen.json")

        self._print(summary)

    # --- Humanos ------------------------------------------------------------

    def _control_handles(self) -> set[str]:
        return {
            _handle(p.name)
            for p in (EVAL_DIR / "humanos_pre2022").glob("*.pdf")
            if _handle(p.name)
        }

    def _control_rows(self) -> list[dict]:
        extractor = DocumentTextExtractor()
        rows = []
        for pdf in sorted((EVAL_DIR / "humanos_pre2022").glob("*.pdf")):
            raw = extractor.extract(_file_document(pdf)).content
            rows += _rows(ia_dataset.prose_segments(raw), LABEL_HUMAN, "humano_control",
                          pdf.stem, "control", "")
        return rows

    def _corpus_rows(self, control_handles: set[str], max_segments: int) -> list[dict]:
        full_texts = (
            OaiFullText.objects.filter(
                status=OaiFullTextStatus.OK,
                publication_year__lte=MAX_YEAR,
            )
            .select_related("record")
            .order_by("record__source_url")
        )
        documents = [
            ft for ft in full_texts
            if EDUCATION_TITLE_RE.search(ft.record.title)
            and _handle(ft.record.source_url) not in control_handles
        ]
        rng = random.Random(SPLIT_SEED)
        rng.shuffle(documents)
        splits = _assign_splits([str(ft.record_id) for ft in documents], HUMAN_SPLITS)
        per_document = -(-max_segments // max(len(documents), 1))
        rows = []

        for ft in documents:
            raw = "".join(
                OaiFullTextChunk.objects.filter(record_id=ft.record_id)
                .order_by("position")
                .values_list("text", flat=True)
            )
            segments = ia_dataset.prose_segments(raw)
            rng.shuffle(segments)
            doc_id = "upeu_" + (_handle(ft.record.source_url) or str(ft.record_id)).replace("/", "_")
            rows += _rows(segments[:per_document], LABEL_HUMAN, "humano_corpus", doc_id,
                          splits[str(ft.record_id)], "")

        if len(rows) > max_segments:
            rng.shuffle(rows)
            rows = rows[:max_segments]
        return rows

    # --- IA -----------------------------------------------------------------

    def _api_rows(self, source: str) -> list[dict]:
        folder = EVAL_DIR / "ia_api" / source
        plan_path = folder / "plan.json"
        if not plan_path.exists():
            return []
        plan = {spec["id"]: spec for spec in json.loads(plan_path.read_text(encoding="utf-8"))["textos"]}
        doc_ids = sorted(p.stem for p in folder.glob(f"{source}_api_*.txt"))
        rng = random.Random(f"{SPLIT_SEED}-{source}")
        shuffled = doc_ids[:]
        rng.shuffle(shuffled)
        n_val = round(len(shuffled) * API_VAL_RATIO)
        val = set(shuffled[:n_val])
        rows = []

        for doc_id in doc_ids:
            raw = (folder / f"{doc_id}.txt").read_text(encoding="utf-8")
            segments = ia_dataset.prose_segments(ia_dataset.generated_text_via_pdf(raw))
            style = plan.get(doc_id, {}).get("estilo", "")
            split = "val" if doc_id in val else "train"
            origin = f"{source}_api"
            rows += _rows(segments, LABEL_AI, origin, doc_id, split, style)
            if split == "train":
                rows += _rows(ia_dataset.overlapping_windows(segments), LABEL_AI, origin,
                              doc_id, split, style, overlap=True)
        return rows

    def _chat_rows(self) -> list[dict]:
        rows = []
        for path in sorted((EVAL_DIR / "ia_externa").glob("*_[0-9][0-9].txt")):
            source, number = path.stem.split("_")
            raw = path.read_text(encoding="utf-8")
            segments = ia_dataset.prose_segments(ia_dataset.generated_text_via_pdf(raw))
            rows += _rows(segments, LABEL_AI, f"{source}_chat", path.stem, "test_chat",
                          ia_dataset.chat_style(int(number)))

        extractor = DocumentTextExtractor()
        for path in sorted((EVAL_DIR / "ia").glob("*.docx")):
            raw = extractor.extract(_file_document(path)).content
            segments = ia_dataset.prose_segments(raw)
            rows += _rows(segments, LABEL_AI, "ia_docx", path.stem, "test_chat",
                          ia_dataset.STYLE_NORMAL)
        return rows

    # --- Resumen ------------------------------------------------------------

    def _summary(self, rows: list[dict]) -> dict:
        table = defaultdict(lambda: {"segmentos": 0, "documentos": set()})
        for row in rows:
            key = f"{row['origen']}|{row['particion']}|{'solape' if row['solape'] else 'base'}"
            table[key]["segmentos"] += 1
            table[key]["documentos"].add(row["documento"])
        leaks = _document_leaks(rows)
        return {
            "filas": len(rows),
            "por_grupo": {
                key: {"segmentos": v["segmentos"], "documentos": len(v["documentos"])}
                for key, v in sorted(table.items())
            },
            "estilos_ia_test_chat": dict(Counter(
                r["estilo"] for r in rows if r["etiqueta"] == LABEL_AI and r["particion"] == "test_chat"
            )),
            "documentos_en_varias_particiones": leaks,
        }

    def _print(self, summary: dict) -> None:
        self.stdout.write(f"{'origen|partición|tipo':<40} {'segm':>6} {'docs':>5}")
        for key, value in summary["por_grupo"].items():
            self.stdout.write(f"{key:<40} {value['segmentos']:>6} {value['documentos']:>5}")
        self.stdout.write(f"Fugas de documento entre particiones: {len(summary['documentos_en_varias_particiones'])}")
        probe = summary["prueba_formato"]
        verdict = "OK" if probe["auroc"] <= FORMAT_PROBE_MAX_AUROC else "FALLA"
        self.stdout.write(f"Prueba de solo formato: AUROC {probe['auroc']:.3f} ({verdict}, máx {FORMAT_PROBE_MAX_AUROC})")
        self.stdout.write(f"Referencia, solo estilo (señal legítima): AUROC {probe['auroc_estilo']:.3f}")
        for name, auc in probe["auroc_por_rasgo"].items():
            self.stdout.write(f"  {name:<28} {auc:.3f}")


def _rows(segments, label, origin, document, split, style, *, overlap=False) -> list[dict]:
    return [
        {
            "texto": text,
            "etiqueta": label,
            "origen": origin,
            "documento": document,
            "estilo": style,
            "particion": split,
            "solape": overlap,
        }
        for text in segments
    ]


def _assign_splits(doc_ids: list[str], splits) -> dict[str, str]:
    result = {}
    total = len(doc_ids)
    cursor = 0
    for index, (name, ratio) in enumerate(splits):
        end = total if index == len(splits) - 1 else cursor + round(total * ratio)
        for doc_id in doc_ids[cursor:end]:
            result[doc_id] = name
        cursor = end
    return result


def _document_leaks(rows: list[dict]) -> list[str]:
    splits = defaultdict(set)
    for row in rows:
        splits[row["documento"]].add(row["particion"])
    return sorted(doc for doc, parts in splits.items() if len(parts) > 1)


def _handle(value: str) -> str:
    match = _HANDLE_RE.search(value)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def _file_document(path: Path):
    mime = {
        ".pdf": DocumentTextExtractor.PDF_MIME,
        ".docx": DocumentTextExtractor.DOCX_MIME,
    }[path.suffix.lower()]
    return SimpleNamespace(id=path.name, mime_type=mime, original_file=File(None, name=str(path)))


def format_probe(rows: list[dict]) -> dict:
    """
    Clasificador de SOLO formato (longitudes, puntuación, dígitos...) sobre
    los segmentos base (sin ventanas solapadas ni test_chat), con validación
    cruzada agrupada por documento. Si supera AUROC 0,65, la normalización
    deja pistas de formato que el modelo podría aprender.
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold, cross_val_predict
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    data = [
        r for r in rows
        if not r["solape"] and r["particion"] in {"train", "val", "test"}
    ]
    if len({r["etiqueta"] for r in data}) < 2:
        return {"auroc": 0.0, "auroc_por_rasgo": {}, "segmentos": len(data)}

    features = np.array([ia_dataset.format_features(r["texto"]) for r in data])
    labels = np.array([r["etiqueta"] for r in data])
    groups = np.array([r["documento"] for r in data])
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=2000, class_weight="balanced"),
    )
    scores = cross_val_predict(
        model, features, labels, groups=groups, cv=GroupKFold(n_splits=5),
        method="predict_proba",
    )[:, 1]
    style = np.array([ia_dataset.style_features(r["texto"]) for r in data])
    style_scores = cross_val_predict(
        model, style, labels, groups=groups, cv=GroupKFold(n_splits=5),
        method="predict_proba",
    )[:, 1]
    per_feature = {}
    for names, matrix in ((ia_dataset.FORMAT_FEATURE_NAMES, features),
                          (ia_dataset.STYLE_FEATURE_NAMES, style)):
        for index, name in enumerate(names):
            auc = roc_auc_score(labels, matrix[:, index])
            per_feature[name] = round(max(auc, 1 - auc), 3)
    return {
        "auroc": round(float(roc_auc_score(labels, scores)), 3),
        "auroc_estilo": round(float(roc_auc_score(labels, style_scores)), 3),
        "auroc_por_rasgo": per_feature,
        "segmentos": len(data),
    }
