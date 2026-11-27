from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import AdvisorInviteCode, AdvisorLink, User


class AdvisorLinkInline(admin.TabularInline):
    model = AdvisorLink
    fk_name = "student"
    extra = 0
    fields = ("advisor", "course_name", "academic_period", "is_active", "invite_code", "created_at")
    readonly_fields = ("invite_code", "created_at")
    autocomplete_fields = ("advisor",)
    verbose_name = "Docente / curso"
    verbose_name_plural = "Docentes y cursos del alumno"


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
                    "Los topes y la autorización de cada perfil se configuran "
                    "en «Permisos por rol». Los envíos adicionales se suman al "
                    "tope del perfil de este usuario."
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
    inlines = ()

    def get_inlines(self, request, obj):
        # Los vínculos solo tienen sentido para alumnos ya creados.
        if obj is not None and obj.role == "STUDENT":
            return (AdvisorLinkInline,)
        return ()
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


@admin.register(AdvisorInviteCode)
class AdvisorInviteCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "owner", "course_name", "academic_period", "is_active", "created_at")
    list_filter = ("is_active", "academic_period")
    search_fields = ("code", "course_name", "owner__email", "owner__first_name", "owner__last_name")
    autocomplete_fields = ("owner",)
    readonly_fields = ("created_at",)
    actions = ("regenerate_codes",)

    @admin.action(description="Regenerar los códigos seleccionados")
    def regenerate_codes(self, request, queryset):
        from apps.accounts.services import InviteCodeService

        total = 0
        for invite in queryset.select_related("owner"):
            InviteCodeService(invite.owner).regenerate(invite.pk)
            total += 1

        self.message_user(request, f"{total} código(s) regenerado(s).")


@admin.register(AdvisorLink)
class AdvisorLinkAdmin(admin.ModelAdmin):
    list_display = ("student", "advisor", "course_name", "academic_period", "is_active", "created_at")
    list_filter = ("is_active", "academic_period")
    search_fields = (
        "student__email", "student__first_name", "student__last_name",
        "advisor__email", "advisor__first_name", "advisor__last_name",
        "course_name",
    )
    autocomplete_fields = ("student", "advisor")
    readonly_fields = ("invite_code", "created_at")
