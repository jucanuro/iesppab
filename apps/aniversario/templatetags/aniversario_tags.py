from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

from django import template
from django.conf import settings

register = template.Library()


def url_embebida(url: str) -> str:
    """Convierte un enlace de YouTube o Facebook en su URL de inserción."""
    if not url:
        return ""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    video_id = ""
    if host == "youtu.be":
        video_id = p.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "youtube-nocookie.com"}:
        if p.path == "/watch":
            video_id = (parse_qs(p.query).get("v") or [""])[0]
        else:
            partes = [x for x in p.path.split("/") if x]
            if len(partes) >= 2 and partes[0] in {"embed", "shorts", "live", "v"}:
                video_id = partes[1]
    if video_id:
        limpio = "".join(c for c in video_id if c.isalnum() or c in "-_")
        return f"https://www.youtube-nocookie.com/embed/{limpio}?rel=0"
    if host in {"facebook.com", "fb.watch"}:
        return (
            "https://www.facebook.com/plugins/video.php?show_text=false&href="
            + quote(url.strip(), safe="")
        )
    return ""


@register.filter
def embebido(url):
    return url_embebida(url or "")


@register.filter
def dos_digitos(valor):
    try:
        return f"{int(valor):02d}"
    except (TypeError, ValueError):
        return valor


@register.filter
def get_item(diccionario, clave):
    if not diccionario:
        return None
    return diccionario.get(clave)


@register.simple_tag
def whatsapp_consultas() -> str:
    """Número para el botón flotante de consultas (solo dígitos)."""
    return getattr(settings, "ANIVERSARIO_WHATSAPP", "")
