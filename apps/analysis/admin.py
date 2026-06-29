from django.contrib import admin

from apps.analysis.models import AnalysisJob


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "requested_by",
        "analysis_type",
        "status",
        "attempt_number",
        "current_step",
        "created_at",
    )
    list_filter = (
        "analysis_type",
        "status",
        "created_at",
    )
    search_fields = (
        "document__title",
        "requested_by__username",
        "requested_by__email",
        "celery_task_id",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "celery_task_id",
        "error_code",
        "error_message",
    )
    autocomplete_fields = (
        "document",
        "requested_by",
    )