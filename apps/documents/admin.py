from django.contrib import admin

from apps.documents.models import Document, DocumentText


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "owner",
        "uploaded_by",
        "institution",
        "kind",
        "status",
        "language",
        "created_at",
    )
    list_filter = (
        "institution",
        "kind",
        "status",
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