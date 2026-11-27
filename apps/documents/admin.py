from django.contrib import admin

from apps.documents.models import Document, DocumentText, RolePermission


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "owner",
        "uploaded_by",
        "advisor",
        "institution",
        "kind",
        "status",
        "authorization_status",
        "language",
        "created_at",
    )
    list_filter = (
        "institution",
        "kind",
        "status",
        "authorization_status",
        "language",
        "created_at",
    )
    search_fields = (
        "title",
        "owner__username",
        "owner__email",
        "uploaded_by__username",
        "sha256_hash",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "sha256_hash",
        "file_size_bytes",
        "mime_type",
    )
    autocomplete_fields = (
        "institution",
        "owner",
        "uploaded_by",
        "advisor",
    )


@admin.register(DocumentText)
class DocumentTextAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "word_count",
        "character_count",
        "extraction_engine",
        "extracted_at",
    )
    search_fields = (
        "document__title",
        "content",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )
    autocomplete_fields = ("document",)


@admin.register(RolePermission)
class RolePermissionAdmin(admin.ModelAdmin):
    """
    «Permisos por rol»: una fila por perfil (alumno, exalumno, docente /
    asesor, director, administrador). Solo se editan; las filas se crean al
    migrar.
    """

    list_display = (
        "profile",
        "requires_authorization",
        "requires_first_validation",
        "free_submissions",
        "max_submissions_display",
        "updated_at",
    )
    list_editable = (
        "requires_authorization",
        "requires_first_validation",
        "free_submissions",
    )
    readonly_fields = ("profile", "updated_at")
    fields = (
        "profile",
        "requires_authorization",
        "requires_first_validation",
        "free_submissions",
        "max_submissions",
        "updated_at",
    )

    @admin.display(description="Máximo de envíos")
    def max_submissions_display(self, obj: RolePermission) -> str:
        return "Sin límite" if obj.max_submissions is None else str(obj.max_submissions)

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
