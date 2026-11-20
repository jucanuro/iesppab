from __future__ import annotations

import csv

from django.contrib import admin
from django.http import HttpResponse
from django.utils.html import format_html

from . import services
from .models import MAX_FOTOS, VIDEO_MAX_MB, Candidato, Concurso, FotoCandidato, Voto


@admin.action(description="Exportar resultados a CSV")
def exportar_resultados(modeladmin, request, queryset):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="resultados_aniversario.csv"'
    response.write("﻿")  # BOM para que Excel abra bien las tildes
    writer = csv.writer(response)
    writer.writerow(["Concurso", "Categoría", "Puesto", "Número", "Candidato", "Ciclo", "Carrera", "Votos", "%"])
    for concurso in queryset:
        datos = services.calcular_conteo(concurso)
        candidatos = sorted(
            Candidato.objects.filter(concurso=concurso, activo=True),
            key=lambda c: (c.categoria != Candidato.MISS, datos.get(c.pk, {}).get("puesto", 99), c.numero),
        )
        for c in candidatos:
            d = datos.get(c.pk, {"votos": 0, "pct": 0, "puesto": "-"})
            writer.writerow([
                concurso.nombre, c.get_categoria_display(), d["puesto"], c.numero,
                c.nombres, c.ciclo, c.carrera, d["votos"], d["pct"],
            ])
    return response


@admin.register(Concurso)
class ConcursoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "fecha_inicio", "fecha_cierre", "activo", "mostrar_conteo", "max_votos_por_ip", "total_votos")
    list_filter = ("activo",)
    actions = [exportar_resultados]

    @admin.display(description="votos")
    def total_votos(self, obj):
        return obj.votos.count()


class FotoInline(admin.TabularInline):
    model = FotoCandidato
    extra = 1
    max_num = MAX_FOTOS
    validate_max = True
    fields = ("vista", "imagen", "ajuste", "posicion", "orden")
    readonly_fields = ("vista",)
    verbose_name_plural = f"Fotos (máximo {MAX_FOTOS}; la de menor orden es la principal)"

    @admin.display(description="vista previa")
    def vista(self, obj):
        if obj and obj.pk and obj.imagen:
            return format_html(
                '<div style="width:88px;height:110px;border-radius:8px;overflow:hidden;background:#E6EEEA">'
                '<img src="{}" style="width:100%;height:100%;object-fit:{};object-position:{}"></div>',
                obj.imagen.url, obj.ajuste, obj.posicion,
            )
        return "—"

    def get_formset(self, request, obj=None, **kwargs):
        kwargs["validate_max"] = True
        return super().get_formset(request, obj, **kwargs)


@admin.register(Candidato)
class CandidatoAdmin(admin.ModelAdmin):
    list_display = ("miniatura", "numero", "nombres", "categoria", "ciclo", "carrera", "num_fotos", "tiene_video", "orden", "activo")
    list_display_links = ("miniatura", "nombres")
    list_editable = ("orden", "activo")
    list_filter = ("concurso", "categoria", "ciclo", "activo")
    search_fields = ("nombres", "carrera")
    inlines = [FotoInline]
    fieldsets = (
        (None, {"fields": ("concurso", "categoria", "numero", "nombres", "orden", "activo")}),
        ("Datos", {"fields": ("ciclo", "carrera", "edad", "procedencia", "biografia", "frase")}),
        ("Video de presentación", {
            "fields": ("video_archivo", "video_url"),
            "description": (
                f"Sube un MP4/WebM de hasta {VIDEO_MAX_MB} MB o pega un enlace de YouTube/Facebook. "
                "Las fotos (JPG, PNG o WebP) van abajo; se reducen a 1200 px y se guardan como JPEG."
            ),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("fotos_rel")

    @admin.display(description="foto")
    def miniatura(self, obj):
        foto = obj.foto_principal
        if foto:
            return format_html('<img src="{}" style="height:56px;width:45px;object-fit:cover;border-radius:6px">', foto.imagen.url)
        return "—"

    @admin.display(description="fotos")
    def num_fotos(self, obj):
        return f"{len(obj.fotos)}/{MAX_FOTOS}"

    @admin.display(description="video", boolean=True)
    def tiene_video(self, obj):
        return bool(obj.video_archivo or obj.video_url)


@admin.register(Voto)
class VotoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "miss", "mister", "ip", "creado")
    list_filter = ("concurso",)
    search_fields = ("codigo",)
    date_hierarchy = "creado"
    readonly_fields = [f.name for f in Voto._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Solo superusuarios, para borrar el voto de prueba del despliegue.
        return request.user.is_superuser
