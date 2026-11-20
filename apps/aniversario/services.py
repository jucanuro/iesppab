"""Lógica de negocio de la votación: IP real, token de dispositivo, conteo y registro del voto."""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count

from .models import Candidato, Concurso, Voto

logger = logging.getLogger(__name__)

COOKIE_DISPOSITIVO = "aniv_disp"
SALT_DISPOSITIVO = "aniversario.dispositivo"
ALFABETO_CODIGO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class VotoError(Exception):
    """Error controlado del voto; `mensaje` se muestra al usuario."""

    def __init__(self, mensaje: str, voto: Voto | None = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.voto = voto


class VotacionCerradaError(VotoError):
    pass


class VotoDuplicadoError(VotoError):
    pass


class LimiteIpError(VotoError):
    pass


class SeleccionInvalidaError(VotoError):
    pass


class RateLimitError(VotoError):
    pass


def _cache():
    return caches["aniversario"]


# ---------------------------------------------------------------- IP real

def _ip_valida(valor: str | None) -> str | None:
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        return str(ipaddress.ip_address(valor))
    except ValueError:
        return None


def obtener_ip(request) -> str | None:
    """IP del cliente. Solo se creen los encabezados de Nginx si la conexión
    llega desde un proxy de confianza (o por socket Unix, REMOTE_ADDR vacío)."""
    remoto = request.META.get("REMOTE_ADDR", "")
    confianza = set(getattr(settings, "ANIVERSARIO_PROXIES_CONFIANZA", []))
    if not remoto or remoto in confianza:
        real = _ip_valida(request.META.get("HTTP_X_REAL_IP"))
        if real:
            return real
        reenviada = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if reenviada:
            # Nginx agrega $remote_addr al final: ese es el valor fiable.
            ultima = _ip_valida(reenviada.split(",")[-1])
            if ultima:
                return ultima
    return _ip_valida(remoto)


# ------------------------------------------------------ token de dispositivo

def leer_token(request) -> str | None:
    valor = request.COOKIES.get(COOKIE_DISPOSITIVO)
    if not valor:
        return None
    try:
        return signing.loads(valor, salt=SALT_DISPOSITIVO)
    except signing.BadSignature:
        return None


def nuevo_token() -> str:
    return uuid.uuid4().hex


def poner_cookie_token(response, request, token: str, concurso: Concurso) -> None:
    from django.utils import timezone

    vence = concurso.fecha_cierre + timedelta(days=7)
    max_age = max(int((vence - timezone.now()).total_seconds()), 3600)
    response.set_cookie(
        COOKIE_DISPOSITIVO,
        signing.dumps(token, salt=SALT_DISPOSITIVO),
        max_age=max_age,
        httponly=True,
        secure=request.is_secure(),
        samesite="Lax",
    )


def voto_del_dispositivo(token: str | None) -> Voto | None:
    if not token:
        return None
    return (
        Voto.objects.select_related("miss", "mister")
        .filter(token_dispositivo=token)
        .first()
    )


def hash_user_agent(request) -> str:
    ua = request.META.get("HTTP_USER_AGENT", "")[:512]
    return hashlib.sha256(ua.encode("utf-8", "ignore")).hexdigest() if ua else ""


def generar_codigo() -> str:
    parte = lambda: "".join(secrets.choice(ALFABETO_CODIGO) for _ in range(4))  # noqa: E731
    return f"AN-{parte()}-{parte()}"


# ------------------------------------------------------------------- conteo

def _clave_conteo(concurso_id) -> str:
    return f"aniv:conteo:{concurso_id}"


def calcular_conteo(concurso: Concurso) -> dict[int, dict]:
    """{candidato_id: {votos, pct, puesto}} por agregación, sin contadores mutables."""
    miss = dict(
        Voto.objects.filter(concurso=concurso)
        .values_list("miss_id").annotate(n=Count("id")).values_list("miss_id", "n")
    )
    mister = dict(
        Voto.objects.filter(concurso=concurso)
        .values_list("mister_id").annotate(n=Count("id")).values_list("mister_id", "n")
    )
    resultado: dict[int, dict] = {}
    candidatos = Candidato.objects.filter(concurso=concurso, activo=True).values_list(
        "id", "categoria"
    )
    por_categoria: dict[str, list[tuple[int, int]]] = {Candidato.MISS: [], Candidato.MISTER: []}
    for cid, cat in candidatos:
        votos = (miss if cat == Candidato.MISS else mister).get(cid, 0)
        por_categoria[cat].append((cid, votos))
    for filas in por_categoria.values():
        total = sum(v for _, v in filas)
        for cid, votos in filas:
            resultado[cid] = {
                "votos": votos,
                "pct": round(votos * 100 / total) if total else 0,
                "puesto": 1 + sum(1 for _, otro in filas if otro > votos),
            }
    return resultado


def conteo(concurso: Concurso) -> dict[int, dict]:
    clave = _clave_conteo(concurso.pk)
    datos = _cache().get(clave)
    if datos is None:
        datos = calcular_conteo(concurso)
        _cache().set(clave, datos, getattr(settings, "ANIVERSARIO_CONTEO_TTL", 20))
    return datos


def invalidar_conteo(concurso: Concurso) -> None:
    _cache().delete(_clave_conteo(concurso.pk))


# --------------------------------------------------------------- rate limit

def verificar_rate_limit(ip: str | None) -> None:
    limite = getattr(settings, "ANIVERSARIO_RATE_POR_MINUTO", 10)
    if not ip or limite <= 0:
        return
    clave = f"aniv:rate:{ip}"
    cache = _cache()
    if cache.add(clave, 1, 60):
        return
    try:
        intentos = cache.incr(clave)
    except ValueError:
        cache.set(clave, 1, 60)
        return
    if intentos > limite:
        raise RateLimitError("Demasiados intentos. Espera un minuto y vuelve a intentarlo.")


# --------------------------------------------------------------- votación

@dataclass
class DatosVoto:
    miss_id: int | str | None
    mister_id: int | str | None
    ip: str | None
    token: str
    user_agent_hash: str = ""


class VotoService:
    """Única vía para registrar un voto. Toda la validación ocurre aquí."""

    def __init__(self, concurso: Concurso):
        self.concurso = concurso

    def _candidato(self, valor, categoria: str) -> Candidato:
        try:
            pk = int(valor)
        except (TypeError, ValueError):
            pk = None
        cand = (
            Candidato.objects.filter(pk=pk, concurso=self.concurso, activo=True).first()
            if pk
            else None
        )
        if cand is None or cand.categoria != categoria:
            etiqueta = "candidata a Miss" if categoria == Candidato.MISS else "candidato a Mister"
            raise SeleccionInvalidaError(f"Elige una {etiqueta} válida." if categoria == Candidato.MISS
                                         else f"Elige un {etiqueta} válido.")
        return cand

    def registrar(self, datos: DatosVoto) -> Voto:
        if not datos.token:
            raise VotoError("No se pudo identificar tu equipo. Recarga la página.")
        miss = self._candidato(datos.miss_id, Candidato.MISS)
        mister = self._candidato(datos.mister_id, Candidato.MISTER)
        try:
            with transaction.atomic():
                concurso = Concurso.objects.select_for_update().get(pk=self.concurso.pk)
                estado = concurso.estado()
                if estado == "pendiente":
                    raise VotacionCerradaError("La votación aún no ha comenzado.")
                if estado == "cerrado":
                    raise VotacionCerradaError("La votación ya cerró.")
                existente = voto_del_dispositivo(datos.token)
                if existente:
                    raise VotoDuplicadoError("Este equipo ya votó.", existente)
                if datos.ip and (
                    Voto.objects.filter(concurso=concurso, ip=datos.ip).count()
                    >= concurso.max_votos_por_ip
                ):
                    raise LimiteIpError(
                        "Ya se registró el máximo de votos permitido desde esta conexión."
                    )
                voto = Voto(
                    concurso=concurso,
                    miss=miss,
                    mister=mister,
                    ip=datos.ip,
                    token_dispositivo=datos.token,
                    user_agent_hash=datos.user_agent_hash,
                )
                # La unicidad la garantiza la BD (IntegrityError abajo), no una consulta previa.
                voto.full_clean(exclude=["codigo"], validate_unique=False)
                for _ in range(5):
                    voto.codigo = generar_codigo()
                    if not Voto.objects.filter(codigo=voto.codigo).exists():
                        break
                voto.save()
        except IntegrityError:
            existente = voto_del_dispositivo(datos.token)
            if existente:
                raise VotoDuplicadoError("Este equipo ya votó.", existente)
            logger.exception("Error de integridad al registrar voto de aniversario")
            raise VotoError("No se pudo registrar el voto. Inténtalo de nuevo.")
        except ValidationError as exc:
            raise SeleccionInvalidaError(" ".join(exc.messages))
        # El conteo no se invalida aquí: se refresca solo con su TTL (15–30 s)
        # para que la carga de la votación no recalcule el agregado en cada voto.
        logger.info("Voto de aniversario registrado: %s", voto.codigo)
        return voto
