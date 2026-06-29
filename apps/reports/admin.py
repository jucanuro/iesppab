from django.contrib import admin

from apps.reports.models import (
    AnalysisReport,
    ReportFinding,
    ReportSource,
)


class ReportSourceInline(admin.TabularInline):
    model = ReportSource
    extra = 0
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )


class ReportFindingInline(admin.TabularInline):
    model = ReportFinding
    extra = 0
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )


@admin.register(AnalysisReport)
class AnalysisReportAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "similarity_percent",
        "web_similarity_percent",
        "internal_similarity_percent",
        "ai_probability_percent",
        "risk_level",
        "generated_at",
        "is_final",
    )
    list_filter = (
        "risk_level",
        "is_final",
        "generated_at",
    )
    search_fields = (
        "document__title",
        "document__owner__username",
        "document__owner__email",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "generated_at",
    )
    autocomplete_fields = (
        "document",
        "analysis_job",
    )
    inlines = (
        ReportSourceInline,
        ReportFindingInline,
    )


@admin.register(ReportSource)
class ReportSourceAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "source_type",
        "domain",
        "matched_percent",
        "report",
    )
    list_filter = (
        "source_type",
        "domain",
    )
    search_fields = (
        "title",
        "url",
        "domain",
        "snippet",
    )
    autocomplete_fields = ("report",)


@admin.register(ReportFinding)
class ReportFindingAdmin(admin.ModelAdmin):
    list_display = (
        "report",
        "finding_type",
        "confidence_percent",
        "start_offset",
        "end_offset",
    )
    list_filter = (
        "finding_type",
    )
    search_fields = (
        "text_excerpt",
        "report__document__title",
    )
    autocomplete_fields = (
        "report",
        "source",
    )