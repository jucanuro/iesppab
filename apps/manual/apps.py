from django.apps import AppConfig


class ManualConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.manual"
    verbose_name = "Manual de usuario"
