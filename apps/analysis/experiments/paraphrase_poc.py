"""
FASE 1 — Prueba de concepto de detección de parafraseo con embeddings
semánticos (NO integrado al pipeline real, NO toca la base de datos).

Ejecutar (con el venv activado):

    python apps/analysis/experiments/paraphrase_poc.py

Modelo elegido: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

Por qué este y no otro:
- Es multilingüe (incluye español) y está entrenado específicamente para
  "paraphrase mining" / similitud semántica, que es exactamente la tarea que
  queremos evaluar.
- OJO: pese al nombre "MiniLM", el peso real en disco es ~470MB, NO ~120MB
  como se asumía al plantear este POC. La confusión viene de las versiones
  "all-MiniLM-L6-v2" / "paraphrase-MiniLM-L6-v2" (solo inglés, ~90MB): esas
  usan un vocabulario pequeño. La versión MULTILINGÜE necesita un vocabulario
  compartido entre 50+ idiomas (~250k tokens de XLM-R), y esa tabla de
  embeddings de entrada por sí sola pesa ~380MB, independientemente de que el
  transformer en sí solo tenga 12 capas de 384 dimensiones (liviano). Este
  costo de vocabulario es inherente a CUALQUIER modelo multilingüe decente
  (se probó también "hackathon-pln-es/paraphrase-spanish-distilroberta",
  ~499MB — un modelo específico de español, pero NO más liviano).
- Comparado con esa alternativa específica de español, MiniLM-L12 multilingüe
  gana en velocidad de inferencia en CPU: su costo por capa depende de
  dim_oculta^2 (384^2 = 147k), y aunque tiene el doble de capas (12 vs 6), el
  costo total (12 * 147k ≈ 1.77M) es ~2x menor que el de distilroberta-base
  (6 capas * 768^2 = 3.54M). Además está mantenido por la propia organización
  sentence-transformers y es el estándar de facto para similitud semántica
  multilingüe (a diferencia del modelo de un hackathon de 2021, sin
  mantenimiento posterior conocido).
- Reutiliza el mismo stack (torch/transformers) que ya está instalado para
  el detector de perplejidad (apps/analysis/engines/perplexity_detector.py),
  así que no se duplica esa dependencia pesada.

Dependencia nueva: sentence-transformers (no estaba instalado). Se agregó
ÚNICAMENTE a requirements-local.txt junto a scikit-learn/scipy (sus
dependencias transitivas que tampoco estaban instaladas). NO se tocó
requirements.txt.
"""

from __future__ import annotations

import time

# Grupos de prueba: (tema, original, parafraseado a mano, no relacionado).
# Los originales son párrafos académicos cortos en español; los parafraseados
# cambian vocabulario y estructura sintáctica mantenendo el significado; los
# "no relacionados" son de otro tema pero con el mismo registro académico
# (para no medir solo "detecta que el tema es distinto" trivialmente).
TEST_CASES = [
    {
        "tema": "Deserción universitaria",
        "original": (
            "La deserción universitaria en América Latina constituye un "
            "problema estructural que trasciende las capacidades "
            "individuales del estudiante, pues involucra factores "
            "económicos, sociales e institucionales que rara vez son "
            "abordados de manera integral por las políticas públicas de "
            "educación superior."
        ),
        "parafraseado": (
            "El abandono de los estudios superiores en la región "
            "latinoamericana no depende únicamente de las aptitudes de cada "
            "alumno, sino que responde a una combinación de condiciones "
            "económicas, sociales e institucionales que las políticas "
            "educativas suelen atender de forma parcial y fragmentada."
        ),
        "no_relacionado": (
            "El calentamiento global ha provocado un incremento sostenido "
            "en la frecuencia de eventos climáticos extremos, como sequías "
            "prolongadas e inundaciones repentinas, que afectan directamente "
            "a la producción agrícola en zonas vulnerables."
        ),
    },
    {
        "tema": "Aprendizaje basado en proyectos",
        "original": (
            "El aprendizaje basado en proyectos favorece el desarrollo de "
            "competencias transversales en los estudiantes, ya que los "
            "enfrenta a problemas reales que exigen la integración de "
            "conocimientos de distintas disciplinas."
        ),
        "parafraseado": (
            "Cuando los alumnos trabajan mediante proyectos, adquieren "
            "habilidades transversales con mayor facilidad, pues deben "
            "resolver situaciones concretas que requieren combinar saberes "
            "provenientes de diversas áreas del conocimiento."
        ),
        "no_relacionado": (
            "La resistencia bacteriana a los antibióticos representa uno de "
            "los mayores desafíos para la salud pública mundial, debido al "
            "uso indiscriminado de estos fármacos tanto en la medicina "
            "humana como en la producción ganadera."
        ),
    },
    {
        "tema": "IA generativa y originalidad académica",
        "original": (
            "La inteligencia artificial generativa ha transformado la "
            "manera en que se produce contenido escrito, planteando nuevos "
            "retos para la evaluación académica de la originalidad en los "
            "trabajos de los estudiantes."
        ),
        "parafraseado": (
            "Las herramientas de IA capaces de generar texto han modificado "
            "radicalmente los procesos de creación de contenidos, lo cual "
            "obliga a repensar cómo se evalúa la autenticidad de las "
            "producciones escritas del alumnado."
        ),
        "no_relacionado": (
            "La biodiversidad marina en los arrecifes de coral se encuentra "
            "amenazada por el aumento de la temperatura oceánica, un "
            "fenómeno que provoca el blanqueamiento masivo de estos "
            "ecosistemas."
        ),
    },
    {
        "tema": "Metodología cualitativa",
        "original": (
            "La metodología cualitativa permite comprender en profundidad "
            "los significados que los actores sociales atribuyen a sus "
            "prácticas cotidianas, a diferencia de los enfoques "
            "cuantitativos centrados en la generalización estadística."
        ),
        "parafraseado": (
            "A diferencia de los métodos cuantitativos, orientados a "
            "generalizar resultados mediante estadística, el enfoque "
            "cualitativo busca entender con mayor profundidad el sentido "
            "que las personas dan a sus acciones diarias."
        ),
        "no_relacionado": (
            "El sistema financiero peruano ha mostrado una notable "
            "resiliencia frente a los choques externos, en parte gracias a "
            "la solidez de sus reservas internacionales y a una política "
            "monetaria prudente."
        ),
    },
]

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def main() -> None:
    t0 = time.time()
    from sentence_transformers import SentenceTransformer, util

    print(f"Cargando modelo '{MODEL_NAME}'...")
    model = SentenceTransformer(MODEL_NAME)
    print(f"Modelo cargado en {time.time() - t0:.1f}s\n")

    rows = []
    for case in TEST_CASES:
        texts = [case["original"], case["parafraseado"], case["no_relacionado"]]
        embeddings = model.encode(texts, convert_to_tensor=True, normalize_embeddings=True)
        sim_parafraseo = util.cos_sim(embeddings[0], embeddings[1]).item()
        sim_no_relacionado = util.cos_sim(embeddings[0], embeddings[2]).item()
        rows.append((case["tema"], sim_parafraseo, sim_no_relacionado))

    header = f"{'Tema':<38} {'orig vs parafraseo':>20} {'orig vs no relacionado':>24}"
    print(header)
    print("-" * len(header))
    for tema, sim_p, sim_u in rows:
        print(f"{tema:<38} {sim_p:>20.3f} {sim_u:>24.3f}")

    avg_p = sum(r[1] for r in rows) / len(rows)
    avg_u = sum(r[2] for r in rows) / len(rows)
    print("-" * len(header))
    print(f"{'PROMEDIO':<38} {avg_p:>20.3f} {avg_u:>24.3f}")

    print(
        "\nRangos esperados (hipótesis de partida): parafraseo ~0.70-0.90, "
        "no relacionado ~0.10-0.30."
    )
    gap = avg_p - avg_u
    print(f"Separación promedio (parafraseo - no relacionado): {gap:.3f}")


if __name__ == "__main__":
    main()
