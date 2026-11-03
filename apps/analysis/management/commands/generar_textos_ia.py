from __future__ import annotations

import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.analysis import ia_dataset

DEFAULT_OUTPUT = Path(settings.BASE_DIR) / "eval_ia" / "ia_api"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Modelos flash y flash-lite del plan gratuito. La cuota diaria es por modelo
# (GenerateRequestsPerDayPerProjectPerModel-FreeTier, ~20 al día), así que
# al agotarse en uno se pasa al siguiente.
DEFAULT_MODELS = (
    "gemini-3.6-flash,gemini-3.7-flash,gemini-3.8-flash,"
    "gemini-3.5-flash-lite,gemini-3.1-flash-lite"
)
MAX_RETRIES = 6
WAIT_FOR_QUOTA_SECONDS = 3600


class DailyQuotaExceeded(Exception):
    pass


class Command(BaseCommand):
    help = (
        "Genera los textos de IA para entrenar el clasificador: escribe el plan "
        "de prompts (tema × sección × estilo, semilla fija) y, con --fuente gemini, "
        "pide cada texto a Gemini. Se puede interrumpir y retomar: los textos "
        "ya guardados no se vuelven a pedir. Solo para uso local."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--fuente", choices=["gemini", "claude"], required=True)
        parser.add_argument("--cantidad", type=int, default=200)
        parser.add_argument("--salida", default=str(DEFAULT_OUTPUT))
        parser.add_argument("--solo-plan", action="store_true")
        parser.add_argument("--modelos", default=DEFAULT_MODELS)
        parser.add_argument(
            "--intervalo",
            type=float,
            default=7.0,
            help="Segundos mínimos entre peticiones (7 s ≈ 8 por minuto).",
        )
        parser.add_argument("--env-file", default=".env.local")
        parser.add_argument(
            "--esperar",
            action="store_true",
            help="Si todos los modelos se quedan sin cuota, duerme una hora y reintenta en vez de terminar.",
        )

    def handle(self, *args, **options) -> None:
        source = options["fuente"]
        out_dir = Path(options["salida"]) / source
        out_dir.mkdir(parents=True, exist_ok=True)
        seed = ia_dataset.PLAN_SEED_GEMINI if source == "gemini" else ia_dataset.PLAN_SEED_CLAUDE
        plan = ia_dataset.build_plan(options["cantidad"], seed=seed, prefix=f"{source}_api")
        plan_path = out_dir / "plan.json"
        plan_path.write_text(
            json.dumps(
                {"semilla": seed, "fuente": source, "textos": [s.as_dict() for s in plan]},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        self.stdout.write(f"Plan de {len(plan)} prompts en {plan_path}")

        if options["solo_plan"] or source != "gemini":
            return

        api_key = self._api_key(options["env_file"])
        all_models = [m.strip() for m in options["modelos"].split(",") if m.strip()]
        models = list(all_models)
        pending = [spec for spec in plan if not (out_dir / f"{spec.id}.txt").exists()]
        self.stdout.write(f"Pendientes: {len(pending)}")
        models_path = out_dir / "modelos.json"
        used = json.loads(models_path.read_text(encoding="utf-8")) if models_path.exists() else {}
        last_request = 0.0

        for number, spec in enumerate(pending, start=1):
            while models:
                try:
                    text, last_request = self._generate(
                        spec.prompt, api_key, models[0], options["intervalo"], last_request
                    )
                    break
                except DailyQuotaExceeded as exc:
                    self.stdout.write(self.style.WARNING(f"{models[0]}: sin cuota ({exc})."))
                    models.pop(0)
                if not models and options["esperar"]:
                    self.stdout.write("Todos los modelos sin cuota: espero una hora.")
                    time.sleep(WAIT_FOR_QUOTA_SECONDS)
                    models = list(all_models)
            if not models:
                self.stdout.write(self.style.WARNING("Todos los modelos sin cuota. Retoma mañana."))
                return
            (out_dir / f"{spec.id}.txt").write_text(text, encoding="utf-8")
            used[spec.id] = models[0]
            models_path.write_text(json.dumps(used, indent=1), encoding="utf-8")
            self.stdout.write(f"[{number}/{len(pending)}] {spec.id} ({models[0]}): {len(text.split())} palabras")

    def _api_key(self, env_file: str) -> str:
        from dotenv import dotenv_values

        key = dotenv_values(env_file).get("GEMINI_API_KEY") or ""
        if not key:
            raise CommandError(f"No hay GEMINI_API_KEY en {env_file}.")
        return key

    def _generate(
        self,
        prompt: str,
        api_key: str,
        model: str,
        interval: float,
        last_request: float,
    ) -> tuple[str, float]:
        import requests

        for attempt in range(MAX_RETRIES):
            wait = interval - (time.monotonic() - last_request)
            if wait > 0:
                time.sleep(wait)
            last_request = time.monotonic()
            try:
                response = requests.post(
                    GEMINI_URL.format(model=model),
                    headers={"x-goog-api-key": api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"thinkingConfig": {"thinkingLevel": "low"}},
                    },
                    timeout=300,
                )
            except requests.RequestException as exc:
                self.stderr.write(f"  error de red ({type(exc).__name__}), reintento")
                time.sleep(30 * (attempt + 1))
                continue

            if response.status_code == 200:
                data = response.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
                if text.strip():
                    return text.strip(), last_request
                self.stderr.write("  respuesta vacía, reintento")
                continue

            error = response.json().get("error", {}) if response.content else {}
            message = str(error.get("message", ""))[:200]
            if response.status_code == 429:
                metrics = _quota_metrics(error)
                daily = any("PerDay" in metric for metric in metrics)
                if daily:
                    raise DailyQuotaExceeded(", ".join(metrics) or message)
                delay = _retry_delay(error) or 60
                self.stderr.write(f"  429 {', '.join(metrics)}")
                self.stderr.write(f"  429, espero {delay:.0f} s")
                time.sleep(delay)
                continue
            if response.status_code in {500, 502, 503, 504}:
                self.stderr.write(f"  {model}: {response.status_code}, reintento")
                time.sleep(30 * (attempt + 1))
                continue
            raise CommandError(f"Gemini respondió {response.status_code}: {message}")

        # Un modelo saturado (503 sostenido) se trata como sin cuota: se pasa
        # al siguiente y este se reintenta en otra corrida.
        raise DailyQuotaExceeded(f"{model} no respondió tras {MAX_RETRIES} intentos")


def _quota_metrics(error: dict) -> list[str]:
    return [
        str(violation.get("quotaId") or violation.get("quotaMetric") or "")
        for detail in error.get("details", [])
        for violation in detail.get("violations", [])
    ]


def _retry_delay(error: dict) -> float | None:
    for detail in error.get("details", []):
        delay = detail.get("retryDelay")
        if delay:
            return float(str(delay).rstrip("s") or 0) + 1
    return None
