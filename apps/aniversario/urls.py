"""Rutas públicas del aniversario (montadas en la raíz de su host)."""
from django.urls import path

from . import views

app_name = "aniversario"

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("candidato/<int:pk>/", views.candidato, name="candidato"),
    path("votar/", views.votar_miss, name="votar"),
    path("votar/mister/", views.votar_mister, name="votar_mister"),
    path("votar/confirmar/", views.confirmar, name="confirmar"),
    path("api/conteo/", views.api_conteo, name="api_conteo"),
]
