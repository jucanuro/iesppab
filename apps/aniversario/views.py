from __future__ import annotations

from urllib.parse import urlencode

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .models import Candidato, Concurso


def _concurso_o_404() -> Concurso:
    concurso = Concurso.vigente()
    if concurso is None:
        raise Http404("No hay un concurso activo.")
    return concurso


def _candidatos(concurso, categoria):
    return list(
        Candidato.objects.filter(concurso=concurso, categoria=categoria, activo=True)
        .order_by("orden", "numero")
    )


def _contexto_base(concurso, **extra):
    datos = {"concurso": concurso, "estado": concurso.estado()}
    datos["conteo"] = services.conteo(concurso) if concurso.mostrar_conteo else {}
    datos.update(extra)
    return datos


def _id_valido(valor, candidatos) -> int | None:
    try:
        pk = int(valor)
    except (TypeError, ValueError):
        return None
    return pk if any(c.pk == pk for c in candidatos) else None


def _con_token(request, response, concurso, token):
    """Garantiza que el equipo tenga su cookie de dispositivo firmada."""
    if services.leer_token(request) != token:
        services.poner_cookie_token(response, request, token, concurso)
    return response


def _token(request) -> str:
    return services.leer_token(request) or services.nuevo_token()


def _gracias(request, concurso, voto, token, status=200):
    response = render(
        request,
        "aniversario/gracias.html",
        _contexto_base(concurso, voto=voto),
        status=status,
    )
    return _con_token(request, response, concurso, token)


@require_GET
def inicio(request):
    concurso = _concurso_o_404()
    return render(
        request,
        "aniversario/inicio.html",
        _contexto_base(
            concurso,
            misses=_candidatos(concurso, Candidato.MISS),
            misters=_candidatos(concurso, Candidato.MISTER),
        ),
    )


@require_GET
def candidato(request, pk):
    concurso = _concurso_o_404()
    cand = get_object_or_404(Candidato, pk=pk, concurso=concurso, activo=True)
    campo = "miss" if cand.es_miss else "mister"
    destino = reverse("aniversario:votar") if cand.es_miss else reverse("aniversario:votar_mister")
    return render(
        request,
        "aniversario/candidato.html",
        _contexto_base(
            concurso,
            c=cand,
            url_elegir=f"{destino}?{campo}={cand.pk}",
            url_absoluta=request.build_absolute_uri(),
            og_imagen=request.build_absolute_uri(cand.foto_1.url) if cand.foto_1 else "",
        ),
    )


def _paso(request, categoria):
    concurso = _concurso_o_404()
    token = _token(request)
    voto = services.voto_del_dispositivo(token)
    if voto:
        return _gracias(request, concurso, voto, token)
    misses = _candidatos(concurso, Candidato.MISS)
    misters = _candidatos(concurso, Candidato.MISTER)
    miss_id = _id_valido(request.GET.get("miss"), misses)
    mister_id = _id_valido(request.GET.get("mister"), misters)
    if categoria == Candidato.MISTER and miss_id is None:
        # Sin Miss elegida se vuelve al paso 1, conservando el Mister si vino de un perfil.
        url = reverse("aniversario:votar")
        return redirect(f"{url}?mister={mister_id}" if mister_id else url)
    lista = misses if categoria == Candidato.MISS else misters
    response = render(
        request,
        "aniversario/votar_paso.html",
        _contexto_base(
            concurso,
            categoria=categoria,
            es_miss=categoria == Candidato.MISS,
            candidatos=lista,
            seleccion=miss_id if categoria == Candidato.MISS else mister_id,
            miss_id=miss_id,
            mister_id=mister_id,
        ),
    )
    return _con_token(request, response, concurso, token)


@require_GET
def votar_miss(request):
    return _paso(request, Candidato.MISS)


@require_GET
def votar_mister(request):
    return _paso(request, Candidato.MISTER)


@require_http_methods(["GET", "POST"])
def confirmar(request):
    concurso = _concurso_o_404()
    token = _token(request)
    voto = services.voto_del_dispositivo(token)
    if voto:
        return _gracias(request, concurso, voto, token)

    fuente = request.POST if request.method == "POST" else request.GET
    misses = _candidatos(concurso, Candidato.MISS)
    misters = _candidatos(concurso, Candidato.MISTER)
    miss_id = _id_valido(fuente.get("miss"), misses)
    mister_id = _id_valido(fuente.get("mister"), misters)
    error = None

    if request.method == "POST":
        ip = services.obtener_ip(request)
        try:
            services.verificar_rate_limit(ip)
            voto = services.VotoService(concurso).registrar(
                services.DatosVoto(
                    miss_id=fuente.get("miss"),
                    mister_id=fuente.get("mister"),
                    ip=ip,
                    token=token,
                    user_agent_hash=services.hash_user_agent(request),
                )
            )
            return _gracias(request, concurso, voto, token)
        except services.VotoDuplicadoError as exc:
            return _gracias(request, concurso, exc.voto, token)
        except services.VotoError as exc:
            error = exc.mensaje
    else:
        if miss_id is None:
            url = reverse("aniversario:votar")
            return redirect(f"{url}?mister={mister_id}" if mister_id else url)
        if mister_id is None:
            return redirect(f"{reverse('aniversario:votar_mister')}?miss={miss_id}")

    por_id = {c.pk: c for c in misses + misters}
    query = urlencode({k: v for k, v in (("miss", miss_id), ("mister", mister_id)) if v})
    response = render(
        request,
        "aniversario/confirmar.html",
        _contexto_base(
            concurso,
            miss=por_id.get(miss_id),
            mister=por_id.get(mister_id),
            error=error,
            url_cambiar_miss=f"{reverse('aniversario:votar')}?{query}",
            url_cambiar_mister=f"{reverse('aniversario:votar_mister')}?{query}",
        ),
        status=400 if error else 200,
    )
    return _con_token(request, response, concurso, token)


@require_GET
def api_conteo(request):
    concurso = _concurso_o_404()
    if not concurso.mostrar_conteo:
        return JsonResponse({"visible": False, "candidatos": {}})
    datos = services.conteo(concurso)
    response = JsonResponse(
        {"visible": True, "candidatos": {str(k): v for k, v in datos.items()}}
    )
    response["Cache-Control"] = "public, max-age=15"
    return response


def no_encontrado(request, exception=None):
    return render(request, "aniversario/404.html", status=404)


def error_servidor(request):
    return render(request, "aniversario/500.html", status=500)


def csrf_failure(request, reason="", *args, **kwargs):
    """CSRF_FAILURE_VIEW: en el host del aniversario usa su propia página; en
    cualquier otro host delega en la vista de originalidad sin cambios."""
    if getattr(request, "es_aniversario", False):
        return render(
            request,
            "aniversario/csrf.html",
            {"concurso": Concurso.vigente()},
            status=403,
        )
    from apps.core.views import csrf_failure as csrf_originalidad

    return csrf_originalidad(request, reason, *args, **kwargs)
