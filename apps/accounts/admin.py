from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    model = User

    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "institution",
        "is_active",
        "is_enabled",
        "student_status",
        "is_staff",
    )
    list_filter = (
        "role",
        "student_status",
        "institution",
        "is_active",
        "is_enabled",
        "is_staff",
        "is_superuser",
    )
    search_fields = (
        "username",
        "email",
        "first_name",
        "last_name",
        "document_number",
    )
    ordering = ("username",)

    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Datos institucionales",
            {
                "fields": (
                    "institution",
                    "role",
                    "document_number",
                    "phone",
                    "must_change_password",
                    "is_enabled",
                )
            },
        ),
        (
            "Envíos y autorización",
            {
                "description": (
                    "Un exalumno validado sube y analiza sin autorización del "
                    "asesor. Los envíos adicionales se suman al tope del rol "
                    "(docente 5, exalumno 3)."
                ),
                "fields": (
                    "student_status",
                    "alumni_validated_at",
                    "alumni_validated_by",
                    "extra_submissions",
                ),
            },
        ),
    )

    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Datos institucionales",
            {
                "fields": (
                    "email",
                    "institution",
                    "role",
                    "document_number",
                    "phone",
                    "is_enabled",
                )
            },
        ),
    )

    readonly_fields = ("last_login", "date_joined", "alumni_validated_by")
    actions = ("mark_as_validated_alumni",)

    @admin.action(description="Marcar como egresado validado")
    def mark_as_validated_alumni(self, request, queryset):
        from apps.documents.services import SubmissionAuthorizationService

        service = SubmissionAuthorizationService(request.user)
        marked = 0

        for student in queryset.filter(role="STUDENT"):
            service.mark_alumni(student.pk)
            marked += 1

        self.message_user(request, f"{marked} alumno(s) marcados como egresados.")