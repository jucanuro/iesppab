from __future__ import annotations

import csv

from django.contrib import admin
from django.http import HttpResponse
from django.utils.html import format_html

from . import services
from .models import Candidato, Concurso, Voto


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


@admin.register(Candidato)
class CandidatoAdmin(admin.ModelAdmin):
    list_display = ("miniatura", "numero", "nombres", "categoria", "ciclo", "carrera", "orden", "activo")
    list_display_links = ("miniatura", "nombres")
    list_editable = ("orden", "activo")
    list_filter = ("concurso", "categoria", "ciclo", "activo")
    search_fields = ("nombres", "carrera")
    readonly_fields = ("vista_previa",)
    fieldsets = (
        (None, {"fields": ("concurso", "categoria", "numero", "nombres", "orden", "activo")}),
        ("Datos", {"fields": ("ciclo", "carrera", "edad", "procedencia", "biografia", "frase")}),
        ("Fotos y video", {
            "fields": ("foto_1", "foto_2", "vista_previa", "video_url"),
            "description": "Fotos JPG, PNG o WebP. Se reducen a 1200 px y se guardan como JPEG.",
        }),
    )

    @admin.display(description="foto")
    def miniatura(self, obj):
        if obj.foto_1:
            return format_html('<img src="{}" style="height:56px;width:45px;object-fit:cover;border-radius:6px">', obj.foto_1.url)
        return "—"

    @admin.display(description="vista previa")
    def vista_previa(self, obj):
        fotos = [f for f in (obj.foto_1, obj.foto_2) if f]
        if not fotos:
            return "Sin fotos"
        return format_html(
            "".join('<img src="{}" style="height:200px;margin-right:8px;border-radius:8px">' for _ in fotos),
            *[f.url for f in fotos],
        )


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
