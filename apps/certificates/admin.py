from django.contrib import admin

from apps.certificates.models import Certificate


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "report",
        "issued_by",
        "is_active",
        "created_at",
    )
    list_filter = (
        "is_active",
        "created_at",
    )
    search_fields = (
        "code",
        "verification_hash",
        "report__document__title",
        "issued_by__username",
        "issued_by__email",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "verification_hash",
        "revoked_at",
    )
    autocomplete_fields = (
        "report",
        "issued_by",
    )