from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analysis", "0009_web_cache"),
    ]

    operations = [
        migrations.AddField(
            model_name="oairecord",
            name="extra_metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Datos de la fuente que no caben en los campos OAI, p. ej. "
                    "los enlaces directos a archivos que entrega la API de "
                    "ALICIA."
                ),
                verbose_name="Metadatos adicionales",
            ),
        ),
    ]
