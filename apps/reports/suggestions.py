from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.reports.models import AnalysisReport


def build_improvement_suggestions(report: "AnalysisReport") -> list[dict]:
    """
    Genera recomendaciones pedagógicas según el nivel de similitud e IA
    del reporte. Orientadas a buena práctica académica, no a evadir el
    detector.
    """

    suggestions: list[dict] = []

    if report.similarity_level == "alto":
        suggestions.append(
            {
                "title": "Cita tus fuentes en formato APA",
                "description": (
                    "Incluye la referencia completa de cada fuente consultada "
                    "y respeta el formato de citación exigido por tu programa."
                ),
                "icon": "cite",
            }
        )
        suggestions.append(
            {
                "title": "Usa comillas en las citas textuales",
                "description": (
                    "Si reproduces un fragmento exacto de otro autor, enciérralo "
                    "entre comillas e indica la página de origen."
                ),
                "icon": "quote",
            }
        )
        suggestions.append(
            {
                "title": "Parafrasea con tus propias palabras",
                "description": (
                    "Reescribe la idea completa en tu propia estructura de "
                    "redacción, en vez de cambiar solo palabras sueltas del texto original."
                ),
                "icon": "rewrite",
            }
        )
        suggestions.append(
            {
                "title": "Agrega tu interpretación personal",
                "description": (
                    "Después de cada idea citada o parafraseada, suma un análisis "
                    "propio que explique su relevancia para tu trabajo."
                ),
                "icon": "analysis",
            }
        )

    elif report.similarity_level == "moderado":
        suggestions.append(
            {
                "title": "Revisa las secciones resaltadas",
                "description": (
                    "Confirma que cada fragmento marcado esté correctamente citado "
                    "y que la fuente aparezca en tu bibliografía."
                ),
                "icon": "cite",
            }
        )

    if report.ai_level == "alto":
        suggestions.append(
            {
                "title": "Varía la extensión de tus oraciones",
                "description": (
                    "Alterna oraciones cortas y largas, evita patrones repetitivos "
                    "y revisa el ritmo de lectura de tu texto."
                ),
                "icon": "rewrite",
            }
        )
        suggestions.append(
            {
                "title": "Incluye ejemplos y datos propios",
                "description": (
                    "Refuerza tus ideas con ejemplos, cifras o experiencias concretas "
                    "que reflejen tu propio análisis del tema."
                ),
                "icon": "analysis",
            }
        )
        suggestions.append(
            {
                "title": "Evita frases de relleno genéricas",
                "description": (
                    "Revisa expresiones muy generales o repetitivas y reemplázalas "
                    "por tu propia voz de escritura."
                ),
                "icon": "quote",
            }
        )

    if report.similarity_level == "bajo" and report.ai_level == "bajo":
        suggestions.append(
            {
                "title": "Buen trabajo de redacción original",
                "description": (
                    "Tu documento muestra niveles bajos de similitud y de patrones "
                    "de IA. Sigue citando tus fuentes para mantener este nivel."
                ),
                "icon": "analysis",
            }
        )

    return suggestions[:5]
