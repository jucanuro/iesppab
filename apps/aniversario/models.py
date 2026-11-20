from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

FOTO_MAX_LADO = 1200
FOTO_CALIDAD = 80
FOTO_EXTENSIONES = ["jpg", "jpeg", "png", "webp"]
MAX_FOTOS = 5
VIDEO_EXTENSIONES = ["mp4", "webm"]
VIDEO_MAX_MB = 60
VIDEO_MIME = {"video/mp4", "video/webm", "video/quicktime"}


def ruta_foto(instance, filename: str) -> str:
    return f"aniversario/candidatos/{uuid.uuid4().hex}.jpg"


def ruta_video(instance, filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".") or "mp4"
    return f"aniversario/videos/{uuid.uuid4().hex}.{extension}"


def ruta_bases(instance, filename: str) -> str:
    return f"aniversario/bases/{uuid.uuid4().hex}.pdf"


def validar_imagen(archivo) -> None:
    """Comprueba con Pillow que el archivo sea realmente una imagen admitida."""
    from PIL import Image, UnidentifiedImageError

    try:
        archivo.seek(0)
        with Image.open(archivo) as img:
            formato = (img.format or "").upper()
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValidationError("El archivo no es una imagen válida.") from exc
    finally:
        try:
            archivo.seek(0)
        except Exception:
            pass
    if formato not in {"JPEG", "PNG", "WEBP"}:
        raise ValidationError("Solo se admiten fotos JPG, PNG o WebP.")


def validar_video(archivo) -> None:
    """Tamaño máximo y tipo real del archivo (python-magic), no solo la extensión."""
    if getattr(archivo, "_committed", False):
        return
    if archivo.size and archivo.size > VIDEO_MAX_MB * 1024 * 1024:
        raise ValidationError(f"El video no puede pesar más de {VIDEO_MAX_MB} MB.")
    try:
        import magic

        archivo.seek(0)
        mime = magic.from_buffer(archivo.read(4096), mime=True)
        archivo.seek(0)
    except ImportError:  # pragma: no cover - python-magic está en requirements
        return
    if mime not in VIDEO_MIME:
        raise ValidationError("El archivo no es un video MP4 o WebM válido.")


def optimizar_imagen(archivo) -> ContentFile:
    """Reescala a FOTO_MAX_LADO px de lado largo y guarda como JPEG ~80 %."""
    from PIL import Image, ImageOps

    archivo.seek(0)
    with Image.open(archivo) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            fondo = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                fondo.paste(img, mask=img.split()[-1])
            else:
                fondo.paste(img.convert("RGB"))
            img = fondo
        elif img.mode == "L":
            img = img.convert("RGB")
        img.thumbnail((FOTO_MAX_LADO, FOTO_MAX_LADO), Image.LANCZOS)
        salida = BytesIO()
        img.save(salida, format="JPEG", quality=FOTO_CALIDAD, optimize=True, progressive=True)
    return ContentFile(salida.getvalue(), name="foto.jpg")


class Concurso(models.Model):
    nombre = models.CharField("nombre", max_length=160)
    fecha_inicio = models.DateTimeField("inicio de la votación")
    fecha_cierre = models.DateTimeField("cierre de la votación")
    video_archivo = models.FileField(
        "video de presentación (archivo MP4/WebM)",
        upload_to=ruta_video,
        blank=True,
        validators=[FileExtensionValidator(VIDEO_EXTENSIONES), validar_video],
        help_text="Opcional. Hasta %(mb)s MB. Si se sube archivo, tiene prioridad sobre el enlace." % {"mb": VIDEO_MAX_MB},
    )
    video_url = models.URLField(
        "o enlace de YouTube/Facebook", blank=True
    )
    bases_pdf = models.FileField(
        "bases (PDF)",
        upload_to=ruta_bases,
        blank=True,
        validators=[FileExtensionValidator(["pdf"])],
    )
    mostrar_conteo = models.BooleanField("mostrar conteo en vivo", default=True)
    max_votos_por_ip = models.PositiveIntegerField(
        "máximo de votos por IP",
        default=1,
        validators=[MinValueValidator(1)],
        help_text=(
            "En el WiFi del campus o con datos móviles (CGNAT) muchos equipos "
            "comparten IP: súbelo si hay rechazos legítimos."
        ),
    )
    activo = models.BooleanField("activo", default=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "concurso"
        verbose_name_plural = "concursos"
        ordering = ["-fecha_inicio"]

    def __str__(self) -> str:
        return self.nombre

    def clean(self):
        if self.fecha_inicio and self.fecha_cierre and self.fecha_cierre <= self.fecha_inicio:
            raise ValidationError("El cierre debe ser posterior al inicio.")

    @classmethod
    def vigente(cls) -> "Concurso | None":
        return cls.objects.filter(activo=True).order_by("-fecha_inicio").first()

    def estado(self, ahora=None) -> str:
        ahora = ahora or timezone.now()
        if ahora < self.fecha_inicio:
            return "pendiente"
        if ahora >= self.fecha_cierre:
            return "cerrado"
        return "abierto"

    @property
    def abierto(self) -> bool:
        return self.estado() == "abierto"


class Candidato(models.Model):
    MISS = "miss"
    MISTER = "mister"
    CATEGORIAS = [(MISS, "Miss"), (MISTER, "Mister")]

    concurso = models.ForeignKey(
        Concurso, on_delete=models.CASCADE, related_name="candidatos"
    )
    categoria = models.CharField("categoría", max_length=6, choices=CATEGORIAS)
    numero = models.PositiveSmallIntegerField("número")
    nombres = models.CharField("nombres y apellidos", max_length=120)
    ciclo = models.CharField("ciclo", max_length=10)
    carrera = models.CharField("carrera", max_length=120)
    edad = models.PositiveSmallIntegerField("edad", null=True, blank=True)
    procedencia = models.CharField("procedencia", max_length=120, blank=True)
    biografia = models.TextField("biografía", blank=True)
    frase = models.CharField("frase", max_length=200, blank=True)
    video_archivo = models.FileField(
        "video (archivo MP4/WebM)",
        upload_to=ruta_video,
        blank=True,
        validators=[FileExtensionValidator(VIDEO_EXTENSIONES), validar_video],
        help_text="Opcional. Hasta %(mb)s MB. Si se sube archivo, tiene prioridad sobre el enlace." % {"mb": VIDEO_MAX_MB},
    )
    video_url = models.URLField(
        "o enlace de YouTube/Facebook", blank=True
    )
    orden = models.PositiveSmallIntegerField("orden", default=0)
    activo = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "candidato"
        verbose_name_plural = "candidatos"
        ordering = ["categoria", "orden", "numero"]
        constraints = [
            models.UniqueConstraint(
                fields=["concurso", "categoria", "numero"],
                name="aniv_candidato_numero_unico",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_categoria_display()} N.º {self.numero:02d} · {self.nombres}"

    @property
    def es_miss(self) -> bool:
        return self.categoria == self.MISS

    @property
    def primer_nombre(self) -> str:
        return (self.nombres.split() or [""])[0]

    @property
    def fotos(self) -> list:
        """Fotos en orden; usa la caché de prefetch_related si existe."""
        return sorted(self.fotos_rel.all(), key=lambda f: (f.orden, f.pk or 0))

    @property
    def foto_principal(self):
        fotos = self.fotos
        return fotos[0] if fotos else None


class FotoCandidato(models.Model):
    candidato = models.ForeignKey(
        Candidato, on_delete=models.CASCADE, related_name="fotos_rel"
    )
    imagen = models.ImageField(
        "foto",
        upload_to=ruta_foto,
        validators=[FileExtensionValidator(FOTO_EXTENSIONES)],
    )
    orden = models.PositiveSmallIntegerField("orden", default=0)

    class Meta:
        verbose_name = "foto"
        verbose_name_plural = "fotos (máximo %d)" % MAX_FOTOS
        ordering = ["orden", "id"]

    def __str__(self) -> str:
        return f"Foto {self.orden} de {self.candidato_id}"

    def clean(self):
        if self.imagen and not getattr(self.imagen, "_committed", True):
            validar_imagen(self.imagen)
        if self.candidato_id:
            otras = FotoCandidato.objects.filter(candidato_id=self.candidato_id).exclude(pk=self.pk)
            if otras.count() >= MAX_FOTOS:
                raise ValidationError(f"Cada candidato puede tener como máximo {MAX_FOTOS} fotos.")

    def save(self, *args, **kwargs):
        if self.imagen and not getattr(self.imagen, "_committed", True):
            optimizada = optimizar_imagen(self.imagen)
            self.imagen.save(Path(self.imagen.name).name, optimizada, save=False)
        super().save(*args, **kwargs)


@receiver(post_delete, sender=FotoCandidato)
def _borrar_archivo_foto(sender, instance, **kwargs):
    """También cubre el borrado en cascada al eliminar un candidato."""
    if instance.imagen:
        instance.imagen.delete(save=False)


class Voto(models.Model):
    concurso = models.ForeignKey(
        Concurso, on_delete=models.PROTECT, related_name="votos"
    )
    miss = models.ForeignKey(
        Candidato, on_delete=models.PROTECT, related_name="votos_miss",
        limit_choices_to={"categoria": Candidato.MISS},
    )
    mister = models.ForeignKey(
        Candidato, on_delete=models.PROTECT, related_name="votos_mister",
        limit_choices_to={"categoria": Candidato.MISTER},
    )
    codigo = models.CharField("comprobante", max_length=12, unique=True)
    ip = models.GenericIPAddressField("IP", null=True, blank=True)
    token_dispositivo = models.CharField("token de dispositivo", max_length=64, unique=True)
    user_agent_hash = models.CharField("huella del navegador", max_length=64, blank=True)
    creado = models.DateTimeField("fecha", auto_now_add=True)

    class Meta:
        verbose_name = "voto"
        verbose_name_plural = "votos"
        ordering = ["-creado"]
        indexes = [
            models.Index(fields=["ip"], name="aniv_voto_ip_idx"),
            models.Index(fields=["creado"], name="aniv_voto_creado_idx"),
        ]

    def __str__(self) -> str:
        return self.codigo

    def clean(self):
        errores = {}
        if self.miss_id and self.miss.categoria != Candidato.MISS:
            errores["miss"] = "La elección de Miss debe ser una candidata a Miss."
        if self.mister_id and self.mister.categoria != Candidato.MISTER:
            errores["mister"] = "La elección de Mister debe ser un candidato a Mister."
        for campo in ("miss", "mister"):
            cand = getattr(self, campo, None) if getattr(self, f"{campo}_id") else None
            if cand and (not cand.activo or cand.concurso_id != self.concurso_id):
                errores.setdefault(campo, "El candidato no participa en este concurso.")
        if errores:
            raise ValidationError(errores)
